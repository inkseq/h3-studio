"""Role-aware, detached creator projections; no provider or database operations."""

import copy
import json
from types import SimpleNamespace
import unittest
from unittest import mock

from studio_platform.auth import Principal
from studio_platform.generation_services import GenerationRead
from studio_platform import quality_views as views
from studio_platform.quality_tiers import QualitySelectionError


PROFILE = "h3-pruned-rank8-int8-sdpa-p4-29gib-controls-v6"
OTHER = "h3-pruned-rank8-int8-pro6000-sdpa-p4-controls-v2"
MODEL = "MiniMax-H3-Pruned-Rank8-INT8"
RECIPE = "h3-base-fl2va-v1"
ADMIN = Principal("owner", "browser", role="admin", identity_source="canonical")
USER = Principal("owner", "browser", role="user", identity_source="canonical")
MACHINE = Principal("owner", "pat", role="admin", machine=True)


class QualityViewTests(unittest.TestCase):
    def test_capability_version_known_dto_slots_are_neutral_without_rehashing(self):
        from studio_platform.capabilities import VERSION, public_version
        from studio_platform.repository import request_hash

        compiled = {
            "capabilities_version": VERSION,
            "recipe_id": RECIPE,
            "request": {"prompt": VERSION, "model": MODEL},
        }
        original_hash = request_hash(compiled)
        saved = {
            "capabilities_version": VERSION,
            "request_hash": original_hash,
            "input_hash": "original-input-hash",
            "prompt": VERSION,
            "user_metadata": {"capabilities_version": VERSION},
            "draft": {
                "capabilities_version": VERSION,
                "recipe_id": RECIPE,
                "prompt": VERSION,
                "controls": {"steps": 9},
            },
            "items": [
                {
                    "plan": {
                        "plan_id": "plan",
                        "request_hash": original_hash,
                        "capabilities_version": VERSION,
                        "request": compiled,
                        "effective_request": {
                            "capabilities_version": VERSION,
                            "prompt": VERSION,
                        },
                    }
                }
            ],
        }
        original = copy.deepcopy(saved)
        alias = public_version(VERSION)
        for principal in (USER, MACHINE, None):
            with self.subTest(principal=principal):
                caps = views.capabilities({"capabilities_version": VERSION}, principal)
                self.assertEqual(caps["capabilities_version"], alias)
                shown = views.envelope(saved, principal)
                self.assertEqual(shown["capabilities_version"], alias)
                self.assertEqual(shown["draft"]["capabilities_version"], alias)
                plan = shown["items"][0]["plan"]
                self.assertEqual(plan["capabilities_version"], alias)
                self.assertEqual(plan["request"]["capabilities_version"], alias)
                self.assertEqual(
                    plan["effective_request"]["capabilities_version"], alias
                )
                self.assertEqual(shown["prompt"], VERSION)
                self.assertEqual(shown["draft"]["prompt"], VERSION)
                self.assertEqual(shown["user_metadata"], original["user_metadata"])
                self.assertEqual(shown["request_hash"], original_hash)
                self.assertEqual(plan["request_hash"], original_hash)
                self.assertEqual(shown["input_hash"], original["input_hash"])
                self.assertEqual(views.envelope(shown, principal), shown)
        self.assertEqual(
            views.capabilities({"capabilities_version": VERSION}, ADMIN)[
                "capabilities_version"
            ],
            VERSION,
        )
        self.assertEqual(views.envelope(saved, ADMIN), original)
        self.assertEqual(saved, original)
        self.assertEqual(request_hash(compiled), original_hash)

    def test_capability_version_schema_aliases_only_the_declared_version_field(self):
        from studio_platform.capabilities import VERSION, public_version

        raw = {
            "components": {
                "schemas": {
                    "VersionedRequest": {
                        "properties": {
                            "capabilities_version": {
                                "anyOf": [
                                    {"type": "string", "enum": [VERSION]},
                                    {"type": "null"},
                                ],
                                "default": VERSION,
                            },
                            "prompt": {
                                "type": "string",
                                "default": VERSION,
                                "enum": [VERSION],
                            },
                            "model_id": {"enum": ["gemma-4-31b-it"]},
                        }
                    }
                }
            },
            "properties": {"capabilities_version": {"const": VERSION}},
        }
        original = copy.deepcopy(raw)
        for principal in (USER, MACHINE, None):
            with self.subTest(principal=principal):
                shown = views.schema(raw, principal)
                fields = shown["components"]["schemas"]["VersionedRequest"][
                    "properties"
                ]
                self.assertEqual(
                    fields["capabilities_version"]["default"], public_version(VERSION)
                )
                self.assertEqual(
                    fields["capabilities_version"]["anyOf"][0]["enum"],
                    [public_version(VERSION)],
                )
                self.assertEqual(
                    shown["properties"]["capabilities_version"]["const"],
                    public_version(VERSION),
                )
                self.assertEqual(
                    fields["prompt"],
                    original["components"]["schemas"]["VersionedRequest"]["properties"][
                        "prompt"
                    ],
                )
                self.assertEqual(fields["model_id"]["enum"], ["gemma-4-31b-it"])
        self.assertEqual(views.schema(raw, ADMIN), original)
        self.assertEqual(raw, original)

    def test_revision_summary_controls_only_and_cpu_dtos_keep_original_shape(self):
        summary = {
            "id": "revision",
            "version": 2,
            "created_at": 123.0,
            "input_hash": "immutable",
        }
        card = {
            "id": "card",
            "version": 2,
            "revisions": [summary],
            "title": "Original card",
        }
        h3 = {"controls": {"steps": 9, "resolution": "720P"}}
        entity = {"id": "shot", "type": "shot", "data": {"h3": h3}}
        cpu = {
            "recipe_id": "chapter-roughcut-v1",
            "duration": 5,
            "title": "Original render",
        }
        originals = copy.deepcopy((card, entity, cpu))
        for principal in (USER, MACHINE, None):
            with self.subTest(principal=principal):
                self.assertEqual(views.envelope(card, principal), card)
                self.assertEqual(views.revision(summary, principal), summary)
                self.assertEqual(views.selection(h3, principal), h3)
                self.assertEqual(views.entity(entity, principal), entity)
                self.assertEqual(views.selection(cpu, principal), cpu)
                unknown = views.selection(
                    {"recipe_id": "retired-video-recipe"}, principal
                )
                self.assertIsNone(unknown["tier"])
                self.assertEqual(unknown["recipe_id"], "video-unavailable-v1")
        self.assertEqual((card, entity, cpu), originals)

    def test_role_boundary_preserves_user_text_and_unknown_history_has_no_fallback(
        self,
    ):
        value = {
            "deployment_profile_id": PROFILE,
            "recipe_id": RECIPE,
            "model_id": MODEL,
            "prompt": "A sign literally reads " + MODEL,
            "title": PROFILE,
            "controls": {"steps": 9, "model_id": MODEL},
            "inputs": {
                "videos": [
                    {
                        "asset_id": "asset",
                        "include_audio": True,
                        "source_range": {"start": 2, "end": 5},
                    }
                ]
            },
        }
        original = copy.deepcopy(value)
        self.assertEqual(views.selection(value, ADMIN), value)
        for principal in (
            None,
            USER,
            MACHINE,
            Principal("owner", "browser", role="admin", status="suspended"),
        ):
            with self.subTest(principal=principal):
                shown = views.selection(value, principal)
                self.assertEqual(shown["deployment_profile_id"], "quality:standard")
                self.assertEqual(shown["recipe_id"], "video-fl-v1")
                self.assertEqual(shown["tier"], "standard")
                self.assertNotIn("model_id", shown)
                self.assertNotIn("model_id", shown["controls"])
                self.assertEqual(shown["prompt"], value["prompt"])
                self.assertEqual(shown["title"], value["title"])
                self.assertEqual(shown["inputs"], value["inputs"])
        unknown = views.selection(
            {
                "deployment_profile_id": "old-unknown-model-profile",
                "model_id": MODEL,
                "recipe_id": "old-unknown-recipe",
            },
            USER,
        )
        self.assertEqual(unknown["deployment_profile_id"], "quality:unavailable")
        self.assertEqual(unknown["recipe_id"], "video-unavailable-v1")
        self.assertIsNone(unknown["tier"])
        self.assertEqual(value, original)

    def test_generation_read_nested_job_artifact_and_execution_are_role_aware(self):
        read = GenerationRead(mock.Mock(), SimpleNamespace(tenant_id="tenant"))
        artifact = {
            "id": "artifact",
            "job_id": "job",
            "metadata": {
                "kind": "video",
                "size_bytes": 30,
                "sha256": "a" * 64,
                "object_key": "private/key",
                "provider": "r2",
                "model_id": MODEL,
                "deployment_profile_id": PROFILE,
                "runtime": {"model_id": MODEL},
                "duration": 5,
                "validated": True,
            },
        }
        job = {
            "id": "job",
            "status": "succeeded",
            "owner_id": "owner",
            "project_id": "project",
            "plan_id": "plan",
            "request_hash": "hash",
            "error_code": None,
            "request": {
                "recipe_id": RECIPE,
                "deployment_profile_id": PROFILE,
                "request": {
                    "model": MODEL,
                    "backend": "wangp",
                    "mode": "fl",
                    "prompt": "human prompt",
                    "steps": 9,
                },
            },
            "execution_plan": {"backend": "wangp-worker", "enabled": True},
            "result": {"artifact_ids": ["artifact"], "runtime": {"model_id": MODEL}},
        }
        original = copy.deepcopy(job)
        admin = read.public_job(
            job, artifact_records=[artifact], cancellation={}, principal=ADMIN
        )
        self.assertEqual(admin["effective_request"]["model"], MODEL)
        self.assertEqual(admin["artifacts"][0]["metadata"]["model_id"], MODEL)
        for principal in (USER, MACHINE, None):
            public = views.job(
                read.public_job(
                    job,
                    artifact_records=[artifact],
                    cancellation={},
                    principal=principal,
                ),
                principal,
            )
            self.assertEqual(public["deployment_profile_id"], "quality:standard")
            self.assertEqual(public["effective_request"]["prompt"], "human prompt")
            self.assertEqual(public["artifacts"][0]["id"], "artifact")
            self.assertNotIn(MODEL, json.dumps(public))
            self.assertNotIn(PROFILE, json.dumps(public))
            self.assertNotIn("runtime", public["result"])
        execution = {
            "enabled": True,
            "deployment_profile_id": PROFILE,
            "backend": "wangp-worker",
            "timing_hint": {
                "deployment_profile_id": PROFILE,
                "cases": [{"model_id": MODEL}],
            },
        }
        self.assertIn("timing_hint", views.execution(read.execution(execution), ADMIN))
        self.assertNotIn(
            "timing_hint", views.execution(read.execution(execution), USER)
        )
        self.assertEqual(job, original)

    def test_diagnostics_drop_raw_host_and_preserve_finite_policy_failure(self):
        raw = {
            "reason_code": "terms_acceptance_required",
            "profile_id": PROFILE,
            "model_id": MODEL,
            "host": {"machine_id": PROFILE},
            "notice_id": "notice",
            "message": MODEL,
        }
        result = views.diagnostic(raw, USER)
        self.assertEqual(result["reason_code"], "terms_acceptance_required")
        self.assertEqual(result["notice_id"], "notice")
        self.assertIn("current terms", result["messages"]["en"])
        self.assertNotIn(MODEL, json.dumps(result))
        self.assertEqual(views.diagnostic(raw, ADMIN), raw)

    def test_story_revision_take_and_film_walk_only_typed_generation_slots(self):
        h3 = {
            "recipeId": RECIPE,
            "deploymentProfileId": PROFILE,
            "model_id": MODEL,
            "inputMode": "fl",
            "explicitReferences": True,
            "controls": {"steps": 9},
        }
        story = {
            "id": "project",
            "entities": [
                {
                    "id": "shot",
                    "title": MODEL,
                    "data": {
                        "text": PROFILE,
                        "model_id": "a user-defined character field",
                        "h3": h3,
                    },
                }
            ],
            "journey": {
                "storyboard": {
                    "defaults": {"recipe_id": RECIPE, "deployment_profile_id": PROFILE}
                }
            },
        }
        original = copy.deepcopy(story)
        public = views.project(story, USER)
        self.assertEqual(
            public["entities"][0]["data"]["h3"]["deploymentProfileId"],
            "quality:standard",
        )
        self.assertTrue(public["entities"][0]["data"]["h3"]["explicitReferences"])
        self.assertEqual(public["entities"][0]["title"], MODEL)
        self.assertEqual(
            public["entities"][0]["data"]["model_id"], "a user-defined character field"
        )
        revision = {
            "id": "revision",
            "title": "Title",
            "prompt": "Prompt",
            "recipe_id": RECIPE,
            "deployment_profile_id": PROFILE,
            "input_hash": "frozen",
            "composer_snapshot": {
                "bindings": [
                    {
                        "asset_id": "asset",
                        "slot": "videos",
                        "include_audio": True,
                        "model_id": MODEL,
                    }
                ]
            },
            "provenance": [
                {"asset_id": "asset", "source": "upload", "model_id": MODEL}
            ],
        }
        shown = views.revision(revision, USER)
        self.assertEqual(shown["input_hash"], "frozen")
        self.assertEqual(shown["composer_snapshot"]["bindings"][0]["slot"], "videos")
        self.assertNotIn(MODEL, json.dumps(shown))
        taken = views.take({"id": "take", "job_id": "job", "recipe": revision}, USER)
        self.assertEqual(taken["recipe"]["deployment_profile_id"], "quality:standard")
        film = views.film(
            {
                "id": "film",
                "generation": [
                    {
                        "plan_id": "plan",
                        "shot_id": "shot",
                        "job_id": "job",
                        "effective_request": {"model": MODEL, "prompt": "Prompt"},
                    }
                ],
                "chapters": [{"chapter_id": "chapter", "artifact_id": "video"}],
                "export": {"job_id": "export"},
            },
            USER,
        )
        self.assertEqual(film["generation"][0]["shot_id"], "shot")
        self.assertEqual(film["chapters"][0]["artifact_id"], "video")
        self.assertEqual(film["export"]["job_id"], "export")
        self.assertNotIn(MODEL, json.dumps(film))
        self.assertEqual(story, original)

    def test_capabilities_exact_mode_resolver_not_union_and_admin_keeps_raw_profiles(
        self,
    ):
        def support(width):
            return {
                "enabled": True,
                "configured": True,
                "controls": {
                    "width": {"type": "integer", "maximum": width, "description": MODEL}
                },
                "limits": {"max_images": 9, "model_id": MODEL},
                "constraints": {
                    "max_pixels": width,
                    "model_id": MODEL,
                    "input_limits": {"guide_recipe_ids": [RECIPE]},
                },
            }

        raw = {
            "capabilities_version": "opaque-version",
            "default_deployment_profile_id": PROFILE,
            "deployment_profiles": [
                {
                    "id": PROFILE,
                    "label": MODEL,
                    "models": [{"model_id": MODEL}],
                    "generation_support": {"fl": support(480), "ref": support(481)},
                },
                {
                    "id": OTHER,
                    "label": "GPU",
                    "generation_support": {"fl": support(720), "ref": support(721)},
                },
            ],
            "recipes": [
                {
                    "id": RECIPE,
                    "mode": "fl",
                    "label": MODEL,
                    "model_id": MODEL,
                    "source": {"repository": MODEL},
                }
            ],
        }
        original = copy.deepcopy(raw)

        def resolve(settings, tier, mode):
            if tier != "standard":
                raise QualitySelectionError(
                    "quality_tier_unavailable", "Unavailable", 409
                )
            return {"deployment_profile_id": PROFILE if mode == "fl" else OTHER}

        with mock.patch(
            "studio_platform.quality_tiers.resolve_selection", side_effect=resolve
        ):
            public = views.capabilities(raw, USER, settings=object())
        standard = public["deployment_profiles"][1]
        self.assertEqual(
            standard["generation_support"]["fl"]["controls"]["width"]["maximum"], 480
        )
        self.assertEqual(
            standard["generation_support"]["ref"]["controls"]["width"]["maximum"], 721
        )
        self.assertFalse(
            public["deployment_profiles"][0]["generation_support"]["fl"]["enabled"]
        )
        self.assertEqual(
            standard["generation_support"]["fl"]["constraints"]["input_limits"][
                "guide_recipe_ids"
            ],
            ["video-fl-v1"],
        )
        self.assertNotIn(MODEL, json.dumps(public))
        self.assertNotIn(PROFILE, json.dumps(public))
        self.assertEqual(
            views.capabilities(raw, ADMIN)["deployment_profiles"][0]["tier"], "standard"
        )
        self.assertEqual(
            views.capabilities(raw, ADMIN)["deployment_profiles"][0]["label"], MODEL
        )
        self.assertEqual(raw, original)

    def test_unavailable_resolver_never_uses_another_enabled_profile(self):
        raw = {
            "default_deployment_profile_id": PROFILE,
            "deployment_profiles": [
                {"id": OTHER, "generation_support": {"ref": {"enabled": True}}}
            ],
        }
        with mock.patch(
            "studio_platform.quality_tiers.resolve_selection",
            side_effect=QualitySelectionError(
                "quality_tier_unavailable", "Unavailable", 409
            ),
        ):
            public = views.capabilities(raw, USER, settings=object())
        self.assertFalse(
            public["deployment_profiles"][1]["generation_support"]["ref"]["enabled"]
        )

    def test_availability_and_schema_aliases_do_not_touch_assistant_models(self):
        value = {
            "profiles": [
                {
                    "deployment_profile_id": PROFILE,
                    "modes": {
                        "ref": {
                            "state": "ready",
                            "available": True,
                            "recipe_id": "h3-base-ref2va-v1",
                            "reason_code": MODEL,
                        }
                    },
                }
            ]
        }
        with mock.patch(
            "studio_platform.quality_tiers.resolve_selection",
            return_value={"deployment_profile_id": PROFILE},
        ):
            public = views.availability(value, MACHINE, settings=object())
        self.assertEqual(
            public["profiles"][1]["modes"]["ref"]["recipe_id"], "video-ref-v1"
        )
        self.assertNotIn(MODEL, json.dumps(public))
        schema = {
            "properties": {
                "deployment_profile_id": {"enum": [PROFILE]},
                "recipe_id": {"enum": [RECIPE]},
                "model_id": {"enum": ["gemma-4-31b-it"]},
            },
            "$defs": {
                "Video": {
                    "properties": {"model_id": {"enum": [MODEL]}},
                    "required": ["model_id"],
                }
            },
        }
        result = views.schema(schema, USER)
        self.assertEqual(result["properties"]["model_id"]["enum"], ["gemma-4-31b-it"])
        self.assertNotIn("model_id", result["$defs"]["Video"]["properties"])
        self.assertNotIn(PROFILE, json.dumps(result))
        self.assertNotIn(MODEL, json.dumps(result))

    def test_actual_guided_and_quick_chat_schema_containers_are_projected(self):
        from studio_platform.guided_schema import contract
        from studio_platform.quick_chat_response_schema import CardAuthoringResponse
        from studio_platform.quick_chat_schema import (
            CreateCardRequest,
            ReviseCardRequest,
        )

        guided = contract()
        chat = {
            "models": [{"id": "gemma-4-31b-it", "label": "Gemma"}],
            "card_authoring": {
                "create_request": CreateCardRequest.model_json_schema(),
                "revision_request": ReviseCardRequest.model_json_schema(),
                "response": CardAuthoringResponse.model_json_schema(),
                "optimistic_concurrency": {"revision_field": "expected_card_version"},
            },
        }
        original_guided, original_chat = copy.deepcopy(guided), copy.deepcopy(chat)
        for principal in (USER, MACHINE, None):
            with self.subTest(principal=principal):
                shown = views.schema(guided, principal)
                configure = shown["operation_schemas"]["shot.configure_generation"][
                    "properties"
                ]
                self.assertEqual(
                    configure["recipe_id"]["enum"], ["video-fl-v1", "video-ref-v1"]
                )
                self.assertEqual(
                    configure["deployment_profile_id"]["enum"],
                    ["quality:fast", "quality:standard", "quality:refine", None],
                )
                defaults = shown["operation_schemas"]["storyboard.defaults"][
                    "properties"
                ]["value"]["properties"]
                self.assertEqual(
                    defaults["recipe_id"]["enum"], ["video-fl-v1", "video-ref-v1"]
                )
                self.assertNotIn(RECIPE, json.dumps(shown["operation_schemas"]))
                public_chat = views.schema(chat, principal)
                self.assertNotIn(RECIPE, json.dumps(public_chat["card_authoring"]))
                create_recipe = public_chat["card_authoring"]["create_request"][
                    "properties"
                ]["recipe_id"]
                self.assertEqual(
                    create_recipe["anyOf"][0]["enum"], ["video-fl-v1", "video-ref-v1"]
                )
                self.assertIn(None, create_recipe["enum"])
                self.assertEqual(public_chat["models"], chat["models"])
                self.assertEqual(
                    public_chat["card_authoring"]["optimistic_concurrency"],
                    chat["card_authoring"]["optimistic_concurrency"],
                )
        self.assertEqual(views.schema(guided, ADMIN), guided)
        self.assertEqual(views.schema(chat, ADMIN), chat)
        self.assertEqual(guided, original_guided)
        self.assertEqual(chat, original_chat)

    def test_batch_envelopes_project_nested_original_jobs(self):
        saved = {
            "batch": {
                "id": "batch",
                "items": [
                    {
                        "job": {
                            "id": "job",
                            "status": "running",
                            "plan_id": "plan",
                            "result": None,
                            "recipe_id": RECIPE,
                            "deployment_profile_id": PROFILE,
                            "effective_request": {
                                "model": MODEL,
                                "prompt": "Original prompt",
                            },
                        }
                    }
                ],
            },
        }
        value = {"batches": [saved["batch"]], **saved}
        shown = views.envelope(value, MACHINE)
        self.assertEqual(
            shown["batch"]["items"][0]["job"]["deployment_profile_id"],
            "quality:standard",
        )
        self.assertEqual(
            shown["batches"][0]["items"][0]["job"]["effective_request"]["prompt"],
            "Original prompt",
        )
        self.assertNotIn(MODEL, json.dumps(shown))
        self.assertNotIn(PROFILE, json.dumps(shown))
        self.assertEqual(
            value["batch"]["items"][0]["job"]["deployment_profile_id"], PROFILE
        )

    def test_envelope_preserves_all_published_controls_money_progress_and_original_ids(
        self,
    ):
        from studio_platform.inference.wangp_profile_dispatch import control_schema

        published = {name: "user setting" for name in control_schema(PROFILE, "ref")}
        preflight = {
            "items": [
                {
                    "id": "item",
                    "shot_id": "shot",
                    "plan": {
                        "plan_id": "plan",
                        "request_hash": "immutable",
                        "estimate": {
                            "cost_microusd": 123,
                            "final_bill": False,
                            "custom_existing": 4,
                        },
                        "effective_request": {
                            "model": MODEL,
                            "mode": "ref",
                            "controls": published,
                            "prompt": "Prompt",
                        },
                        "execution": {
                            "deployment_profile_id": PROFILE,
                            "quote_known": True,
                            "remaining_window_s": 99,
                        },
                    },
                    "job": {
                        "id": "job",
                        "status": "running",
                        "plan_id": "plan",
                        "project_id": "project",
                        "result": None,
                        "progress": {
                            "stage": "sampling",
                            "steps_done": 3,
                            "steps_total": 9,
                        },
                        "queue_position": 2,
                        "remaining_wait_seconds": 30,
                        "retry_advice": {"safe_to_retry": False, "charged": None},
                        "effective_request": {
                            "model": MODEL,
                            "prompt": "Prompt",
                            "mode": "ref",
                        },
                    },
                }
            ]
        }
        original = copy.deepcopy(preflight)
        shown = views.envelope(preflight, USER)
        plan, job = shown["items"][0]["plan"], shown["items"][0]["job"]
        self.assertEqual(plan["effective_request"]["controls"], published)
        self.assertEqual(plan["estimate"], original["items"][0]["plan"]["estimate"])
        self.assertEqual(plan["request_hash"], "immutable")
        self.assertEqual(plan["execution"]["remaining_window_s"], 99)
        self.assertEqual(job["progress"], original["items"][0]["job"]["progress"])
        self.assertEqual(job["remaining_wait_seconds"], 30)
        self.assertEqual(shown["items"][0]["shot_id"], "shot")
        self.assertNotIn(MODEL, json.dumps(shown))
        self.assertNotIn(PROFILE, json.dumps(shown))
        self.assertEqual(preflight, original)

    def test_nested_error_and_entity_envelopes_keep_user_text_and_hide_managed_details(
        self,
    ):
        raw = {
            "detail": {
                "code": "quality_tier_unavailable",
                "message": MODEL,
                "model_id": MODEL,
                "request_hash": "frozen",
            }
        }
        public = views.envelope(raw, USER)
        self.assertEqual(public["detail"]["request_hash"], "frozen")
        self.assertEqual(public["detail"]["code"], "quality_tier_unavailable")
        self.assertNotIn(MODEL, json.dumps(public))
        conflict = views.envelope(
            {
                "detail": {
                    "code": "document_version_conflict",
                    "message": "Defaults changed",
                    "no_mutation_committed": True,
                }
            },
            USER,
        )
        self.assertEqual(conflict["detail"]["code"], "document_version_conflict")
        self.assertTrue(conflict["detail"]["no_mutation_committed"])
        self.assertEqual(
            views.envelope({"detail": "shot_version_conflict"}, USER)["detail"],
            "shot_version_conflict",
        )
        entity = {
            "entity": {
                "id": "shot",
                "type": "shot",
                "title": MODEL,
                "data": {
                    "h3": {"deploymentProfileId": PROFILE, "recipeId": RECIPE},
                    "restoredRecipeContext": {
                        "original_authoring_context": {
                            "effective": {"deployment_profile_id": PROFILE}
                        }
                    },
                },
            }
        }
        shown = views.envelope(entity, USER)
        self.assertEqual(shown["entity"]["title"], MODEL)
        self.assertEqual(
            shown["entity"]["data"]["h3"]["deploymentProfileId"], "quality:standard"
        )
        self.assertNotIn(PROFILE, json.dumps(shown))

    def test_actual_storyboard_versions_and_layered_defaults_hide_only_video_identity(
        self,
    ):
        recipe = {
            "deployment_profile_id": PROFILE,
            "recipe_id": RECIPE,
            "prompt": "Keep this user prompt",
            "controls": {"steps": 9},
        }
        context = {
            "effective": recipe,
            "global": {"deployment_profile_id": PROFILE},
            "story": {"recipe_id": RECIPE},
            "shot": {"deployment_profile_id": PROFILE},
            "provenance": {
                "deployment_profile_id": "global",
                "recipe_id": "story",
                "controls.steps": "shot",
            },
        }
        raw = {
            "project_id": "project",
            "shot_id": "shot",
            "project_version": 4,
            "versions": [
                {
                    "id": "artifact",
                    "job_id": "job",
                    "recipe_status": "known",
                    "recipe": recipe,
                    "authoring_context": context,
                    "title": "A user title",
                    "annotation_version": 4,
                    "selected": True,
                }
            ],
            "defaults": context,
            "next_offset": 20,
        }
        original = copy.deepcopy(raw)
        for principal in (USER, MACHINE, None):
            shown = views.envelope(raw, principal)
            self.assertEqual(
                shown["versions"][0]["recipe"]["deployment_profile_id"],
                "quality:standard",
            )
            self.assertEqual(shown["versions"][0]["recipe"]["prompt"], recipe["prompt"])
            self.assertEqual(shown["versions"][0]["title"], "A user title")
            self.assertEqual(
                shown["defaults"]["effective"]["deployment_profile_id"],
                "quality:standard",
            )
            self.assertEqual(
                shown["defaults"]["global"]["deployment_profile_id"], "quality:standard"
            )
            self.assertEqual(shown["defaults"]["story"]["recipe_id"], "video-fl-v1")
            self.assertEqual(shown["defaults"]["provenance"], context["provenance"])
            self.assertEqual(shown["next_offset"], 20)
            self.assertNotIn(PROFILE, json.dumps(shown))
            self.assertNotIn(RECIPE, json.dumps(shown))
        self.assertEqual(views.envelope(raw, ADMIN), raw)
        self.assertEqual(raw, original)

    def test_full_admission_context_projects_entities_defaults_versions_and_restored_context(
        self,
    ):
        context = {
            "shot": {
                "id": "shot",
                "type": "shot",
                "title": "A user title",
                "data": {
                    "prompt": "User wrote " + MODEL,
                    "h3": {
                        "recipeId": RECIPE,
                        "deploymentProfileId": PROFILE,
                        "controls": {"steps": 9},
                    },
                },
            },
            "scene": {
                "id": "scene",
                "type": "scene",
                "title": "Scene title",
                "data": {
                    "text": "User scene text",
                    "h3": {"deploymentProfileId": PROFILE},
                },
            },
            "storyboard": {
                "defaults": {"recipe_id": RECIPE, "deployment_profile_id": PROFILE},
                "frame": "A user board label",
            },
            "global_defaults": {
                "deployment_profile_id": PROFILE,
                "controls": {"generate_audio": False},
            },
            "profiles": [
                {
                    "id": "character",
                    "type": "character",
                    "title": "User character",
                    "data": {
                        "description": MODEL,
                        "model_id": "user-defined character metadata",
                        "h3": {"deploymentProfileId": PROFILE},
                    },
                }
            ],
            "links": [
                {
                    "id": "link",
                    "source": "character",
                    "target": "shot",
                    "role": "identity",
                }
            ],
        }
        raw = {
            "versions": [
                {
                    "id": "artifact",
                    "authoring_context": context,
                    "recipe": {
                        "deployment_profile_id": PROFILE,
                        "recipe_id": RECIPE,
                        "prompt": "Original prompt",
                    },
                }
            ],
            "project": {
                "id": "project",
                "entities": [
                    {
                        "id": "restored",
                        "type": "shot",
                        "data": {
                            "restoredRecipeContext": {
                                "artifact_id": "artifact",
                                "original_authoring_context": context,
                            }
                        },
                    }
                ],
            },
        }
        original = copy.deepcopy(raw)
        for principal in (USER, MACHINE, None):
            shown = views.envelope(raw, principal)
            for projected in (
                shown["versions"][0]["authoring_context"],
                shown["project"]["entities"][0]["data"]["restoredRecipeContext"][
                    "original_authoring_context"
                ],
            ):
                self.assertEqual(
                    projected["shot"]["data"]["h3"]["deploymentProfileId"],
                    "quality:standard",
                )
                self.assertEqual(
                    projected["scene"]["data"]["h3"]["deploymentProfileId"],
                    "quality:standard",
                )
                self.assertEqual(
                    projected["global_defaults"]["deployment_profile_id"],
                    "quality:standard",
                )
                self.assertEqual(
                    projected["storyboard"]["defaults"]["recipe_id"], "video-fl-v1"
                )
                self.assertEqual(
                    projected["profiles"][0]["data"]["h3"]["deploymentProfileId"],
                    "quality:standard",
                )
                self.assertEqual(
                    projected["shot"]["data"]["prompt"],
                    context["shot"]["data"]["prompt"],
                )
                self.assertEqual(
                    projected["profiles"][0]["data"]["model_id"],
                    "user-defined character metadata",
                )
                self.assertEqual(projected["profiles"][0]["data"]["description"], MODEL)
                self.assertEqual(projected["links"], context["links"])
                self.assertNotIn(PROFILE, json.dumps(projected))
            self.assertEqual(
                shown["versions"][0]["recipe"]["prompt"], "Original prompt"
            )
        self.assertEqual(views.envelope(raw, ADMIN), raw)
        self.assertEqual(raw, original)

    def test_actual_timeline_record_projects_revision_preflight_submission_but_keeps_chat_model(
        self,
    ):
        raw = {
            "events": [
                {
                    "id": "event1",
                    "seq": 1,
                    "type": "card.created",
                    "record": {
                        "id": "revision",
                        "card_id": "card",
                        "input_hash": "hash",
                        "recipe_id": RECIPE,
                        "deployment_profile_id": PROFILE,
                        "prompt": "Unchanged user prompt",
                        "controls": {"steps": 9},
                    },
                },
                {
                    "id": "event2",
                    "seq": 2,
                    "type": "preflight.created",
                    "record": {
                        "id": "preflight",
                        "items": [
                            {
                                "id": "item",
                                "plan": {
                                    "plan_id": "plan",
                                    "effective_request": {
                                        "model": MODEL,
                                        "prompt": "Unchanged user prompt",
                                    },
                                    "execution": {"deployment_profile_id": PROFILE},
                                },
                            }
                        ],
                    },
                },
                {
                    "id": "event3",
                    "seq": 3,
                    "type": "submission.created",
                    "record": {
                        "id": "submission",
                        "items": [
                            {
                                "job": {
                                    "id": "job",
                                    "status": "running",
                                    "plan_id": "plan",
                                    "result": None,
                                    "recipe_id": RECIPE,
                                    "deployment_profile_id": PROFILE,
                                    "effective_request": {
                                        "model": MODEL,
                                        "prompt": "Unchanged user prompt",
                                    },
                                }
                            }
                        ],
                    },
                },
                {
                    "id": "event4",
                    "seq": 4,
                    "type": "turn.created",
                    "record": {
                        "id": "turn",
                        "model_id": "gemma-4-31b-it",
                        "text": "User deliberately wrote " + MODEL,
                        "next_settings": {
                            "deployment_profile_id": PROFILE,
                            "recipe_id": RECIPE,
                        },
                    },
                },
            ],
            "has_more": True,
            "next_cursor": "original-cursor",
        }
        original = copy.deepcopy(raw)
        shown = views.envelope(raw, MACHINE)
        self.assertNotIn(PROFILE, json.dumps(shown))
        self.assertNotIn(RECIPE, json.dumps(shown))
        self.assertEqual(
            shown["events"][0]["record"]["prompt"], "Unchanged user prompt"
        )
        self.assertNotIn(
            "model",
            shown["events"][1]["record"]["items"][0]["plan"]["effective_request"],
        )
        self.assertEqual(
            shown["events"][2]["record"]["items"][0]["job"]["deployment_profile_id"],
            "quality:standard",
        )
        self.assertEqual(shown["events"][3]["record"]["model_id"], "gemma-4-31b-it")
        self.assertEqual(
            shown["events"][3]["record"]["text"], raw["events"][3]["record"]["text"]
        )
        self.assertEqual(shown["next_cursor"], "original-cursor")
        self.assertEqual(raw, original)


if __name__ == "__main__":
    unittest.main()
