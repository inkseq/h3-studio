"""Hidden creator overrides cannot leak through the still-active v6 default."""

import copy
import unittest

import test_quality_authoring as fixture
from studio_platform.api import create_app
from studio_platform.capabilities import compile_request
from studio_platform.creator_controls import CONTROLS_V6, POLICY, V6_POLICY
from studio_platform.film_runs import selected_recipe_current
from studio_platform.quality_authoring import normalize
from studio_platform.repository import Scope
from test_platform_api import generation_request


class CreatorV6Tests(unittest.TestCase):
    def setUp(self):
        self.f = fixture.QualityAuthoringTests(methodName="runTest")
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.addCleanup(self.f.tearDown)

    def test_each_stored_layer_uses_profile_defaults_without_rewriting_source(self):
        f = self.f
        stale = {"steps": 100, "sampler_name": "invalid", "video_tile_size": 999}
        for layer in ("shot", "story", "account"):
            with self.subTest(layer=layer):
                h3 = {
                    "recipeId": fixture.RECIPE,
                    "deploymentProfileId": CONTROLS_V6,
                    "controls": {
                        "seed": "123",
                        "resolution": "768P",
                        "aspect_ratio": "9:16",
                        "generate_audio": False,
                    },
                }
                if layer == "shot":
                    h3["controls"].update(stale)
                defaults = {"controls": stale}
                project = f.seed(h3=h3, defaults=defaults if layer == "story" else None)
                scope = Scope(f.settings.tenant_id, f.user.owner, "__preferences")
                if layer == "account":
                    f.repo.put_document(
                        scope, "storyboard_preferences", "defaults", defaults
                    )
                before = f.read(project)
                account_before = (
                    f.repo.get_document(scope, "storyboard_preferences", "defaults")
                    if layer == "account"
                    else None
                )
                admission = create_app(
                    f.settings, repository=f.repo
                ).state.generation_admission
                result = admission.preflight(
                    f.user, project, "shot-one", expected_version=1
                )
                effective = result["effective_request"]
                self.assertEqual(effective["steps"], 20)
                self.assertEqual(effective["sampler_name"], "euler")
                self.assertEqual(effective["seed"], "123")
                self.assertEqual(effective["resolution"], "768P")
                self.assertEqual(effective["aspect_ratio"], "9:16")
                self.assertIs(effective["generate_audio"], False)
                original = admission.access.plan(f.user, result["plan_id"])["request"]
                self.assertEqual(original["creator_control_policy"], V6_POLICY)
                self.assertEqual(f.read(project), before)
                if layer == "account":
                    self.assertEqual(
                        f.repo.get_document(
                            scope, "storyboard_preferences", "defaults"
                        ),
                        account_before,
                    )

    def test_implicit_default_preflight_and_receipt_freshness_keep_output_choices(self):
        f = self.f
        project = f.seed(
            h3={
                "controls": {
                    "steps": 50,
                    "seed": "12",
                    "aspect_ratio": "9:16",
                    "generate_audio": False,
                }
            }
        )
        admission = create_app(f.settings, repository=f.repo).state.generation_admission
        result = admission.preflight(f.user, project, "shot-one", expected_version=1)
        self.assertEqual(result["effective_request"]["steps"], 20)
        original = admission.access.plan(f.user, result["plan_id"])["request"]
        before = copy.deepcopy(original)
        caps = admission.capabilities_provider()
        self.assertTrue(
            selected_recipe_current(project, "shot-one", original, {}, caps)
        )
        changed = copy.deepcopy(project)
        changed["entities"][-1]["data"]["h3"]["controls"]["steps"] = 100
        self.assertTrue(
            selected_recipe_current(changed, "shot-one", original, {}, caps)
        )
        # A historical v6 take really used its override; changing it still
        # affects freshness. Do not reclassify that take under the new policy.
        historical = copy.deepcopy(original)
        historical.pop("creator_control_policy")
        historical["authoring_recipe"]["controls"]["steps"] = 50
        self.assertTrue(
            selected_recipe_current(project, "shot-one", historical, {}, caps)
        )
        self.assertFalse(
            selected_recipe_current(changed, "shot-one", historical, {}, caps)
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

    def test_historical_or_foreign_policy_receipts_keep_v6_controls(self):
        from studio_platform.creator_controls import effective_creator_project

        project = self.f.seed(
            h3={"deploymentProfileId": CONTROLS_V6, "controls": {"steps": 50}}
        )
        for marker in (None, POLICY, "future-policy"):
            original = {"deployment_profile_id": CONTROLS_V6}
            if marker is not None:
                original["creator_control_policy"] = marker
            rendered, _ = effective_creator_project(project, "shot-one", {}, original)
            self.assertEqual(
                rendered["entities"][-1]["data"]["h3"]["controls"]["steps"], 50
            )

    def test_public_selections_filter_but_explicit_admin_override_is_unchanged(self):
        f = self.f
        stale = {"steps": 50, "duration": 5, "seed": "123", "resolution": "480P"}
        public = generation_request(tier="standard", mode="fl", controls=stale)
        before = copy.deepcopy(public)
        for principal in (f.user, f.admin, f.machine):
            resolved, _ = normalize(f.settings, f.repo, principal, public)
            compiled, _ = compile_request(
                resolved, lambda _: None, backend="wangp-worker"
            )
            self.assertEqual(compiled["request"]["steps"], 20)
        self.assertEqual(public, before)
        internal = generation_request(deployment_profile_id=CONTROLS_V6, controls=stale)
        resolved, selection = normalize(f.settings, f.repo, f.admin, internal)
        self.assertIsNone(selection)
        compiled, _ = compile_request(resolved, lambda _: None, backend="wangp-worker")
        self.assertEqual(compiled["request"]["steps"], 50)
        internal["controls"]["sampler_name"] = "invalid"
        resolved, _ = normalize(f.settings, f.repo, f.admin, internal)
        with self.assertRaises(ValueError):
            compile_request(resolved, lambda _: None, backend="wangp-worker")

    def test_invalid_output_choices_still_fail_native_validation(self):
        for invalid in ({"aspect_ratio": "invalid"}, {"generate_audio": "false"}):
            body = generation_request(tier="standard", mode="fl", controls=invalid)
            resolved, _ = normalize(self.f.settings, self.f.repo, self.f.user, body)
            self.assertEqual(resolved["controls"], invalid)
            with self.assertRaises(ValueError):
                compile_request(resolved, lambda _: None, backend="wangp-worker")


if __name__ == "__main__":
    unittest.main()
