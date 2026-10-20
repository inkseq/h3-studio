"""New managed selections are gated; unchanged history and replay stay intact."""

import copy
from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace
import time
from unittest.mock import patch
import uuid

from sqlalchemy import insert, update

from studio_platform.auth import Principal
from studio_platform.guided import Guided
from studio_platform.guided_schema import operation_schemas
from studio_platform.h3_profile_support import envelope
from studio_platform.identity_schema import metadata, users
from studio_platform.quality_tiers import QualitySelectionError
from studio_platform.quick_chat import QuickChatService
from studio_platform.qualification_profiles import QUEUED_TASK_PROFILE
from studio_platform.repository import NotFound, Scope
from studio_platform.runtime_catalog import (
    engine_manifest,
    get_profile,
    profile_output_delivery,
)
from studio_platform.settings import Settings
from test_platform_api import project
from test_platform_execution_policy import policy
from test_platform_repository import LedgerCase

PROFILE = "h3-pruned-rank8-int8-sdpa-p4-29gib-controls-v6"
OLD_PROFILE = "h3-pruned-rank8-int8-pro6000-sdpa-p4-controls-v2"
RECIPE = "h3-base-fl2va-v1"
RAW = {"recipeId": RECIPE, "deploymentProfileId": PROFILE}
PUBLIC = {"tier": "standard", "mode": "fl"}


class QualityAuthoringTests(LedgerCase):
    def setUp(self):
        super().setUp()
        self.now = time.time()
        self.path = Path(self.temp.name) / "profiles.json"
        self.settings = Settings(
            Path(self.temp.name),
            tenant_id=self.scope.tenant_id,
            execution_profiles_file=self.path,
            default_deployment_profile_id=PROFILE,
            execution_backend="wangp-worker",
            generation_enabled=True,
        )
        value = policy(self.now)
        value.update(
            deployment_profile_id=PROFILE,
            model_id=get_profile(PROFILE)["model_id"],
            backend="wangp-worker",
            recipe_ids=[RECIPE],
            engine_manifest_digest=engine_manifest(PROFILE, "fl").digest,
            output_delivery=profile_output_delivery(PROFILE),
            envelope=envelope(PROFILE, "fl"),
        )
        value["qualification"].update(
            status="runtime_required", profile=QUEUED_TASK_PROFILE
        )
        self.path.write_text(
            json.dumps({"schema_version": 1, "policies": [value]}), encoding="utf-8"
        )
        self.user = Principal("supervan", "browser:supervan", role="user")
        self.admin = Principal("superdan", "browser:superdan", role="admin")
        self.machine = Principal(
            "superdan", "key:admin-owned", role="admin", machine=True
        )
        with self.repo.transaction() as connection:
            metadata.create_all(connection)
            for principal in (self.admin, self.user):
                connection.execute(
                    insert(users).values(
                        id=str(uuid.uuid4()),
                        tenant=self.settings.tenant_id,
                        owner=principal.owner,
                        role=principal.role,
                        status="active",
                        display_name=principal.owner,
                        created_at=self.now,
                        updated_at=self.now,
                    )
                )
        self.service = Guided(
            SimpleNamespace(
                state=SimpleNamespace(repository=self.repo, settings=self.settings)
            )
        )

    def scope_for(self, principal):
        return Scope(
            self.settings.tenant_id, principal.owner, "__projects", principal.actor_id
        )

    def seed(self, principal=None, *, h3=None, defaults=None):
        principal = principal or self.user
        value = project("story-" + uuid.uuid4().hex)
        if h3 is not None:
            value["entities"][-1]["data"]["h3"] = copy.deepcopy(h3)
        if defaults is not None:
            value["journey"] = {"storyboard": {"defaults": copy.deepcopy(defaults)}}
        self.repo.put_document(self.scope_for(principal), "project", value["id"], value)
        return value

    def read(self, value, principal=None):
        return self.repo.get_document(
            self.scope_for(principal or self.user), "project", value["id"]
        )

    def edit(self, value, actions, *, principal=None, key=None):
        return self.service.mutate(
            principal or self.user,
            value["id"],
            {
                "expected_version": self.read(value, principal)["version"],
                "actions": actions,
            },
            key,
        )

    def chat_card(self, *, profiled=False):
        settings = (
            self.settings
            if profiled
            else replace(
                self.settings,
                default_deployment_profile_id=None,
                execution_profiles_file=None,
                execution_backend="mock",
            )
        )
        service = QuickChatService(self.repo, None, settings, None)
        session = service.create_session(self.user, {}, "chat-" + uuid.uuid4().hex)[
            "session"
        ]
        body = {
            "prompt": "An owned offline editing fixture.",
            "recipe_id": RECIPE,
            "controls": {
                "duration": 5,
                "resolution": "480P",
                "steps": 20,
                "seed": "42",
            },
            "inputs": {},
            "copies": 1,
        }
        if profiled:
            with self.repo.transaction() as connection:
                connection.execute(
                    update(users)
                    .where(users.c.owner == self.user.owner)
                    .values(role="admin")
                )
            body["deployment_profile_id"] = OLD_PROFILE
        original = service.save_card(self.user, session["id"], body, "first-card")
        if profiled:
            with self.repo.transaction() as connection:
                connection.execute(
                    update(users)
                    .where(users.c.owner == self.user.owner)
                    .values(role="user")
                )
        from studio_platform.quality_views import revision

        shown = revision(original["revision"], self.user)
        edit = {
            key: copy.deepcopy(shown[key])
            for key in ("prompt", "recipe_id", "controls", "inputs", "copies")
        }
        edit.update(
            controls={**edit["controls"], "seed": "123"},
            expected_card_version=original["card"]["version"],
            source_revision_id=original["revision"]["id"],
        )
        return service, session, original, edit

    def test_new_project_and_full_put_reject_raw_ids_for_users_and_all_machines(self):
        for principal in (self.user, self.machine):
            for managed in ("shot", "defaults"):
                value = project("denied-" + uuid.uuid4().hex)
                if managed == "shot":
                    value["entities"][-1]["data"]["h3"] = dict(RAW)
                else:
                    value["journey"] = {
                        "storyboard": {
                            "defaults": {
                                "recipe_id": RECIPE,
                                "deployment_profile_id": PROFILE,
                            }
                        }
                    }
                with self.subTest(principal=principal.actor_id, managed=managed):
                    with self.assertRaisesRegex(
                        QualitySelectionError, "model_selection_admin_required"
                    ):
                        self.service.mutate(
                            principal, value["id"], value, "new", create=True
                        )
                    with self.assertRaises(NotFound):
                        self.read(value, principal)
                baseline = self.seed(principal)
                incoming = {**value, "id": baseline["id"]}
                before = self.read(baseline, principal)
                with self.assertRaisesRegex(
                    QualitySelectionError, "model_selection_admin_required"
                ):
                    self.service.save(principal, baseline["id"], incoming, 1)
                self.assertEqual(self.read(baseline, principal), before)

    def test_generic_entity_and_journey_writes_refuse_atomically(self):
        value = self.seed()
        before = self.read(value)
        operations = (
            {
                "op": "entity.create",
                "entity": {
                    "id": "new-shot",
                    "type": "shot",
                    "parentId": "scene-one",
                    "title": "New",
                    "data": {"seconds": 5, "h3": dict(RAW)},
                },
            },
            {
                "op": "entity.update",
                "entity_id": "shot-one",
                "patch": {"data": {"h3": dict(RAW)}},
            },
            {
                "op": "journey.update",
                "patch": {
                    "storyboard": {
                        "defaults": {
                            "recipe_id": RECIPE,
                            "deployment_profile_id": PROFILE,
                        }
                    }
                },
            },
        )
        for operation in operations:
            with self.subTest(op=operation["op"]):
                with self.assertRaisesRegex(
                    QualitySelectionError, "model_selection_admin_required"
                ):
                    self.edit(
                        value,
                        [
                            {
                                "op": "project.update",
                                "patch": {"title": "Must roll back"},
                            },
                            operation,
                        ],
                        key="denied-" + operation["op"].replace(".", "-"),
                    )
                self.assertEqual(self.read(value), before)

    def test_public_selection_works_in_generic_and_specific_actions_once(self):
        value = self.seed()
        response = self.edit(
            value,
            [
                {
                    "op": "entity.create",
                    "entity": {
                        "id": "new-shot",
                        "type": "shot",
                        "parentId": "scene-one",
                        "title": "New",
                        "data": {"seconds": 5, "h3": dict(PUBLIC)},
                    },
                },
                {
                    "op": "entity.update",
                    "entity_id": "shot-one",
                    "patch": {
                        "data": {
                            "h3": {
                                "deploymentProfileId": "quality:standard",
                                "recipeId": "video-fl-v1",
                            }
                        }
                    },
                },
                {
                    "op": "journey.update",
                    "patch": {"storyboard": {"defaults": dict(PUBLIC)}},
                },
                {
                    "op": "shot.configure_generation",
                    "shot_id": "new-shot",
                    **PUBLIC,
                    "controls": {"steps": 7},
                },
                {
                    "op": "storyboard.defaults",
                    "value": {**PUBLIC, "controls": {"steps": 9}},
                },
            ],
            key="public-actions",
        )
        saved = response["project"]
        for shot in (item for item in saved["entities"] if item["type"] == "shot"):
            self.assertEqual(shot["data"]["h3"]["deploymentProfileId"], PROFILE)
            self.assertEqual(shot["data"]["h3"]["recipeId"], RECIPE)
        self.assertEqual(saved["entities"][-1]["data"]["h3"]["controls"]["steps"], 7)
        self.assertEqual(
            saved["journey"]["storyboard"]["defaults"],
            {
                "deployment_profile_id": PROFILE,
                "recipe_id": RECIPE,
                "controls": {"steps": 9},
            },
        )
        incoming = self.seed()
        incoming["entities"][-1]["data"]["h3"] = dict(PUBLIC)
        incoming["journey"] = {"storyboard": {"defaults": dict(PUBLIC)}}
        replaced = self.service.save(self.user, incoming["id"], incoming, 1)
        self.assertEqual(replaced["project"]["entities"][-1]["data"]["h3"], RAW)
        created = copy.deepcopy(incoming)
        created["id"] = "public-" + uuid.uuid4().hex
        self.assertEqual(
            self.service.mutate(
                self.user, created["id"], created, "public-create", create=True
            )["project"]["entities"][-1]["data"]["h3"],
            RAW,
        )
        schemas = operation_schemas()
        for op in ("shot.configure_generation", "storyboard.defaults"):
            fields = schemas[op]["properties"]
            if op == "storyboard.defaults":
                fields = fields["value"]["properties"]
            self.assertIn("tier", fields)
            self.assertIn("mode", fields)

    def test_unchanged_legacy_selectors_survive_full_put_and_unrelated_actions(self):
        for identity, recipe in (
            (OLD_PROFILE, RECIPE),
            ("retired-profile", "retired-recipe"),
        ):
            h3 = {
                "deploymentProfileId": identity,
                "recipeId": recipe,
                "controls": {"steps": 13},
            }
            defaults = {
                "deployment_profile_id": OLD_PROFILE,
                "recipe_id": RECIPE,
                "controls": {"steps": 15},
            }
            value = self.seed(h3=h3, defaults=defaults)
            incoming = copy.deepcopy(value)
            incoming["title"] = "Edited title"
            incoming["entities"][-1]["data"]["h3"]["controls"]["steps"] = 14
            with patch(
                "studio_platform.quality_authoring.normalize",
                side_effect=AssertionError("Do not reroute history"),
            ):
                saved = self.service.save(self.user, value["id"], incoming, 1)
                changed = self.edit(
                    value,
                    [
                        {
                            "op": "entity.update",
                            "entity_id": "shot-one",
                            "patch": {"title": "Retitled"},
                        },
                        {"op": "journey.update", "patch": {"stage": 4}},
                    ],
                    key="unrelated",
                )
            self.assertEqual(
                saved["project"]["entities"][-1]["data"]["h3"]["deploymentProfileId"],
                identity,
            )
            self.assertEqual(
                changed["project"]["journey"]["storyboard"]["defaults"], defaults
            )
            self.assertEqual(
                changed["project"]["entities"][-1]["data"]["h3"]["controls"]["steps"],
                14,
            )

    def test_neutral_readback_round_trip_preserves_original_known_and_unknown_binding(
        self,
    ):
        from studio_platform.quality_views import project as public_project

        for identity in (OLD_PROFILE, "retired-profile"):
            value = self.seed(
                h3={"deploymentProfileId": identity, "recipeId": RECIPE},
                defaults={"deployment_profile_id": OLD_PROFILE, "recipe_id": RECIPE},
            )
            shown = public_project(value, self.user)
            shown["title"] = "A title edit while no routing is available"
            self.path.write_text("{}", encoding="utf-8")
            with patch(
                "studio_platform.quality_authoring.normalize",
                side_effect=AssertionError("Do not reinterpret a readback"),
            ):
                saved = self.service.save(self.user, value["id"], shown, 1)
            self.assertEqual(
                saved["project"]["entities"][-1]["data"]["h3"],
                value["entities"][-1]["data"]["h3"],
            )
            self.assertEqual(
                saved["project"]["journey"]["storyboard"]["defaults"],
                value["journey"]["storyboard"]["defaults"],
            )

    def test_raw_admin_authoring_is_fresh_but_original_receipt_replays_after_demotion(
        self,
    ):
        value = project("admin-" + uuid.uuid4().hex)
        value["entities"][-1]["data"]["h3"] = dict(RAW)
        first = self.service.mutate(
            self.admin, value["id"], value, "original-create", create=True
        )
        with self.repo.transaction() as connection:
            connection.execute(
                update(users)
                .where(users.c.owner == self.admin.owner)
                .values(role="user")
            )
        self.path.write_text("{}", encoding="utf-8")
        with patch(
            "studio_platform.quality_authoring.normalize",
            side_effect=AssertionError("Replay before current mapping"),
        ):
            replay = self.service.mutate(
                self.admin, value["id"], value, "original-create", create=True
            )
        self.assertEqual(replay, first)
        self.assertEqual(self.read(value, self.admin)["version"], 1)
        new = copy.deepcopy(value)
        new["id"] = "new-" + uuid.uuid4().hex
        with self.assertRaisesRegex(
            QualitySelectionError, "model_selection_admin_required"
        ):
            self.service.mutate(self.admin, new["id"], new, "new-create", create=True)

    def test_legacy_card_neutral_control_edit_and_replay_keep_original_revision(self):
        service, session, original, body = self.chat_card()
        first = copy.deepcopy(original["revision"])
        result = service.save_card(
            self.user,
            session["id"],
            body,
            "legacy-edit",
            card_id=original["card"]["id"],
        )
        self.assertNotIn("deployment_profile_id", result["revision"])
        self.assertEqual(result["revision"]["recipe_id"], RECIPE)
        self.assertEqual(result["revision"]["controls"]["seed"], "123")
        self.assertEqual(
            service.get_revision(self.user, session["id"], first["id"]), first
        )
        with patch(
            "studio_platform.quality_authoring.normalize",
            side_effect=AssertionError("Original replay first"),
        ):
            replay = service.save_card(
                self.user,
                session["id"],
                body,
                "legacy-edit",
                card_id=original["card"]["id"],
            )
        self.assertEqual(replay, result)

    def test_profiled_card_mode_edit_uses_prior_tier_current_binding_not_raw_profile(
        self,
    ):
        service, session, original, body = self.chat_card(profiled=True)
        result = service.save_card(
            self.user,
            session["id"],
            body,
            "profile-edit",
            card_id=original["card"]["id"],
        )
        self.assertEqual(original["revision"]["deployment_profile_id"], OLD_PROFILE)
        self.assertEqual(result["revision"]["deployment_profile_id"], PROFILE)
        self.assertEqual(
            service.get_revision(self.user, session["id"], original["revision"]["id"]),
            original["revision"],
        )
        changed_mode = {
            **body,
            "recipe_id": "video-ref-v1",
            "expected_card_version": result["card"]["version"],
        }
        with self.assertRaisesRegex(QualitySelectionError, "quality_tier_required"):
            service.save_card(
                self.user,
                session["id"],
                changed_mode,
                "changed-mode",
                card_id=original["card"]["id"],
            )
        with self.assertRaisesRegex(
            QualitySelectionError, "model_selection_admin_required"
        ):
            service.save_card(
                self.user,
                session["id"],
                {**body, "deployment_profile_id": OLD_PROFILE},
                "raw-bypass",
                card_id=original["card"]["id"],
            )

    def test_card_compatibility_requires_same_card_source_and_unknown_profile_refuses(
        self,
    ):
        service, session, original, body = self.chat_card()
        other = service.save_card(
            self.user,
            session["id"],
            {
                **{
                    key: original["revision"][key]
                    for key in ("prompt", "recipe_id", "controls", "inputs", "copies")
                },
                "title": "Another card",
            },
            "other-card",
        )
        with self.assertRaises(NotFound):
            service.save_card(
                self.user,
                session["id"],
                body,
                "wrong-card",
                card_id=other["card"]["id"],
            )
        from studio_platform.quality_authoring import inherit_revision_mode

        with self.assertRaisesRegex(QualitySelectionError, "quality_tier_unavailable"):
            inherit_revision_mode(
                body, {"recipe_id": RECIPE, "deployment_profile_id": "retired-profile"}
            )
        unsafe = {**body, "model_id": "private-model"}
        self.assertEqual(inherit_revision_mode(unsafe, original["revision"]), unsafe)

    def test_explicit_recipe_precedes_old_input_mode_without_weakening_new_mode_conflicts(
        self,
    ):
        from studio_platform.quality_authoring import project as resolve_project
        from studio_platform.quality_views import project as public_project

        value = self.seed(h3={**RAW, "inputMode": "ref"})
        resolved = resolve_project(self.settings, self.repo, self.admin, value)
        self.assertEqual(resolved["entities"][-1]["data"]["h3"]["recipeId"], RECIPE)
        shown = public_project(value, self.user)
        shown["title"] = "Only a title edit"
        saved = self.service.save(self.user, value["id"], shown, 1)
        self.assertEqual(
            saved["project"]["entities"][-1]["data"]["h3"],
            value["entities"][-1]["data"]["h3"],
        )
        bad = copy.deepcopy(value)
        bad["entities"][-1]["data"]["h3"]["mode"] = "ref"
        with self.assertRaisesRegex(
            QualitySelectionError, "quality_selection_conflict"
        ):
            resolve_project(self.settings, self.repo, self.admin, bad)
