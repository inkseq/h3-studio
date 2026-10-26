"""Creator policy is a new-request projection, not a historical job rewrite."""

import copy
import unittest
from unittest.mock import patch

import test_quality_authoring as fixture
from studio_platform.api import create_app
from studio_platform.capabilities import compile_request
from studio_platform.creator_controls import TAOMATE, controls
from studio_platform.quality_authoring import normalize
from studio_platform.repository import Scope
from test_platform_api import generation_request


class CreatorControlsTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture.QualityAuthoringTests(methodName="runTest")
        with patch.object(fixture, "PROFILE", TAOMATE):
            self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.addCleanup(self.fixture.tearDown)

    def test_stale_shot_story_and_account_preflight_with_five_steps_and_hd(self):
        f = self.fixture
        for layer in ("shot", "story", "account"):
            with self.subTest(layer=layer):
                stale = {
                    "steps": 50,
                    "sampler_name": "invalid",
                    "shift_audio": 50,
                    "video_decode": "normal",
                    "video_tile_size": 999,
                    "aspect_ratio": "9:16",
                    "generate_audio": False,
                }
                h3 = {
                    "recipeId": fixture.RECIPE,
                    "deploymentProfileId": TAOMATE,
                    "controls": {"seed": "123", "resolution": "768P"},
                }
                defaults = {"controls": stale}
                if layer == "shot":
                    h3["controls"].update(stale)
                project = f.seed(h3=h3, defaults=defaults if layer == "story" else None)
                if layer == "account":
                    f.repo.put_document(
                        Scope(f.settings.tenant_id, f.user.owner, "__preferences"),
                        "storyboard_preferences",
                        "defaults",
                        defaults,
                    )
                before = f.read(project)
                app = create_app(f.settings, repository=f.repo)
                result = app.state.generation_admission.preflight(
                    f.user, project, "shot-one", expected_version=before["version"]
                )
                effective = result["effective_request"]
                self.assertEqual(effective["steps"], 5)
                self.assertEqual(effective["sampler_name"], "euler")
                self.assertEqual(effective["shift_audio"], 3)
                self.assertEqual(effective["video_tile_size"], 256)
                self.assertEqual(effective["resolution"], "768P")
                self.assertEqual(effective["seed"], "123")
                self.assertEqual(effective["aspect_ratio"], "9:16")
                self.assertIs(effective["generate_audio"], False)
                self.assertEqual(f.read(project), before)

    def test_implicit_default_creator_shot_ignores_stale_hidden_controls(self):
        f = self.fixture
        project = f.seed(
            h3={"controls": {"steps": 50, "sampler_name": "invalid", "seed": "12"}}
        )
        app = create_app(f.settings, repository=f.repo)
        result = app.state.generation_admission.preflight(
            f.user, project, "shot-one", expected_version=1
        )
        self.assertEqual(result["effective_request"]["steps"], 5)
        self.assertEqual(result["effective_request"]["seed"], "12")

    def test_census_uses_compiler_rejections_and_keeps_context_separate(self):
        from tools.audit_creator_controls import refused_fields

        refused, context = refused_fields(
            {"steps": 50, "encoder_device": "cpu", "seed": "123"}
        )
        self.assertCountEqual(refused, ["steps", "encoder_device"])
        self.assertEqual(context, [])
        self.assertEqual(
            refused_fields({"steps": 5, "sampler_name": "er_sde", "shift_video": None}),
            ([], []),
        )

    def test_new_take_freshness_ignores_hidden_controls_but_keeps_creator_changes(self):
        from studio_platform.film_runs import selected_recipe_current

        f = self.fixture
        project = f.seed(
            h3={
                "recipeId": fixture.RECIPE,
                "deploymentProfileId": TAOMATE,
                "controls": {
                    "steps": 50,
                    "seed": "12",
                    "aspect_ratio": "9:16",
                    "generate_audio": False,
                },
            }
        )
        app = create_app(f.settings, repository=f.repo)
        admission = app.state.generation_admission
        result = admission.preflight(f.user, project, "shot-one", expected_version=1)
        original = admission.access.plan(f.user, result["plan_id"])["request"]
        before = copy.deepcopy(original)
        caps = admission.capabilities_provider()
        self.assertTrue(
            selected_recipe_current(project, "shot-one", original, {}, caps)
        )
        for key, value in (
            ("seed", "13"),
            ("aspect_ratio", "16:9"),
            ("generate_audio", True),
        ):
            changed = copy.deepcopy(project)
            changed["entities"][-1]["data"]["h3"]["controls"][key] = value
            self.assertFalse(
                selected_recipe_current(changed, "shot-one", original, {}, caps)
            )
        self.assertEqual(original, before)

    def test_historical_take_comparison_keeps_its_internal_controls(self):
        from studio_platform.creator_controls import effective_creator_project

        f = self.fixture
        project = f.seed(h3={"deploymentProfileId": TAOMATE, "controls": {"steps": 5}})
        original = {"authoring_recipe": {"controls": {"steps": 5}}}
        rendered, _ = effective_creator_project(project, "shot-one", {}, original)
        self.assertEqual(rendered["entities"][-1]["data"]["h3"]["controls"]["steps"], 5)

    def test_public_admin_selection_is_creator_but_explicit_internal_api_stays_strict(
        self,
    ):
        f = self.fixture
        stale = {"steps": 50, "duration": 5, "seed": "123", "resolution": "480P"}
        public = generation_request(tier="standard", mode="fl", controls=stale)
        original = copy.deepcopy(public)
        for principal in (f.user, f.admin, f.machine):
            resolved, _ = normalize(f.settings, f.repo, principal, public)
            compiled, _ = compile_request(
                resolved, lambda _: None, backend="wangp-worker"
            )
            self.assertEqual(compiled["request"]["steps"], 5)
        self.assertEqual(public, original)
        internal = generation_request(deployment_profile_id=TAOMATE, controls=stale)
        resolved, selection = normalize(f.settings, f.repo, f.admin, internal)
        self.assertIsNone(selection)
        with self.assertRaisesRegex(ValueError, "wangp_invalid_steps"):
            compile_request(resolved, lambda _: None, backend="wangp-worker")
        internal["controls"].update(steps=5, sampler_name="er_sde")
        resolved, _ = normalize(f.settings, f.repo, f.admin, internal)
        compiled, _ = compile_request(resolved, lambda _: None, backend="wangp-worker")
        self.assertEqual(compiled["request"]["sampler_name"], "er_sde")

    def test_other_profiles_and_invalid_control_containers_are_not_silently_repaired(
        self,
    ):
        original = {"steps": 50, "duration": 5}
        self.assertEqual(controls(fixture.OLD_PROFILE, original), original)
        self.assertIsNone(controls(TAOMATE, None))

    def test_creator_output_choices_keep_native_validation(self):
        f = self.fixture
        for invalid in ({"aspect_ratio": "invalid"}, {"generate_audio": "false"}):
            body = generation_request(tier="standard", mode="fl", controls=invalid)
            resolved, _ = normalize(f.settings, f.repo, f.user, body)
            self.assertEqual(resolved["controls"], invalid)
            with self.assertRaises(ValueError):
                compile_request(resolved, lambda _: None, backend="wangp-worker")

    def test_census_counts_non_default_aspects_by_layer_without_raw_values(self):
        from unittest.mock import Mock
        from tools.audit_creator_controls import census

        connection = Mock()
        connection.execute.return_value.mappings.return_value = [
            {
                "layer": layer,
                "owner_id": "synthetic",
                "controls": {"aspect_ratio": aspect},
            }
            for layer in ("shot", "story", "account")
            for aspect in ("9:16", "1:1", "16:9", None, "", "PRIVATE_TEXT")
        ]
        result = census(connection, "tenant")
        for bucket in result["layers"].values():
            self.assertEqual(bucket["records_with_non_default_aspect"], 3)
            self.assertEqual(bucket["non_default_aspects"], {"9:16": 1, "1:1": 1})
            self.assertEqual(bucket["records_with_invalid_aspect"], 1)
            self.assertEqual(bucket["records_with_hidden_controls"], 0)
        self.assertNotIn("PRIVATE_TEXT", str(result))
