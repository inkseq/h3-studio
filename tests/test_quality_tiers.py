"""Protected quality routing only; no stock, SDK, runtime, or GPU operations."""

import copy
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest

from studio_platform.capabilities import compile_request
from studio_platform.execution_policy import validate_policy
from studio_platform.h3_profile_support import envelope
from studio_platform.quality_tiers import (
    MAPPING_REVISION,
    MODE_RECIPES,
    PROFILE_TIERS,
    QualitySelectionError,
    model_tier,
    normalize_request,
    profile_alias,
    profile_tier,
    recipe_alias,
    recipe_mode,
    resolve_selection,
)
from studio_platform.qualification_profiles import QUEUED_TASK_PROFILE
from studio_platform.runtime_catalog import (
    PROFILE_IDS,
    engine_manifest,
    get_profile,
    profile_output_delivery,
)
from studio_platform.settings import Settings
from test_platform_api import generation_request
from test_platform_execution_policy import policy

STANDARD = "h3-pruned-rank8-int8-sdpa-p4-29gib-controls-v6"
OTHER_STANDARD = "h3-pruned-rank8-int8-pro6000-sdpa-p4-controls-v2"
REFINE = "h3-unpruned33b-int8-sdpa-p3-144gib-single-controls-v4"


class QualityTierTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "profiles.json"
        self.now = 1792460000.0
        self.settings = Settings(
            Path(self.tmp.name),
            execution_profiles_file=self.path,
            default_deployment_profile_id=STANDARD,
            execution_backend="wangp-worker",
            generation_enabled=True,
        )
        self.values = []

    def add(self, identity, mode, **changes):
        value = policy(self.now)
        value.update(
            deployment_profile_id=identity,
            model_id=get_profile(identity)["model_id"],
            backend="wangp-worker",
            recipe_ids=[MODE_RECIPES[mode]],
            engine_manifest_digest=engine_manifest(identity, mode).digest,
            output_delivery=profile_output_delivery(identity),
            envelope=envelope(identity, mode),
            **changes,
        )
        value["qualification"].update(
            status="runtime_required", profile=QUEUED_TASK_PROFILE
        )
        validate_policy(value)
        self.values.append(value)
        self.write()
        return value

    def write(self):
        self.path.write_text(
            json.dumps({"schema_version": 1, "policies": self.values}),
            encoding="utf-8",
        )

    def resolve(self, tier="standard", mode="ref", **extra):
        return resolve_selection(self.settings, tier, mode, now=self.now, **extra)

    def normalize(self, body, **extra):
        return normalize_request(self.settings, body, now=self.now, **extra)

    def test_explicit_catalog_mapping_is_complete_and_detached_from_profiles(self):
        self.assertEqual(set(PROFILE_TIERS), set(PROFILE_IDS))
        for identity in PROFILE_IDS:
            original = get_profile(identity)
            self.assertEqual(profile_tier(identity), model_tier(original["model_id"]))
            self.assertNotIn("tier", original)
            self.assertEqual(get_profile(identity), original)
        for unknown in (None, [], {}, "unregistered", "quality:standard"):
            self.assertIsNone(profile_tier(unknown))
        self.assertIsNone(model_tier("future-model"))

    def test_ref_only_default_and_aliases_resolve_without_fl_or_stock(self):
        self.add(STANDARD, "ref")
        before = self.path.read_bytes()
        expected = {
            "tier": "standard",
            "mapping_revision": MAPPING_REVISION,
            "mode": "ref",
            "deployment_profile_id": STANDARD,
            "recipe_id": MODE_RECIPES["ref"],
        }
        self.assertEqual(self.resolve(), expected)
        for body in (
            {"tier": "standard", "mode": "ref", "prompt": "Untouched"},
            {
                "deployment_profile_id": "quality:standard",
                "recipe_id": "video-ref-v1",
                "prompt": "Untouched",
            },
            {
                "deployment_profile_id": "quality:standard",
                "recipe_id": MODE_RECIPES["ref"],
                "prompt": "Untouched",
            },
        ):
            original = copy.deepcopy(body)
            result, selection = self.normalize(body, allow_internal=False)
            self.assertEqual(selection, expected)
            self.assertEqual(result["deployment_profile_id"], STANDARD)
            self.assertEqual(result["recipe_id"], MODE_RECIPES["ref"])
            self.assertEqual(result["prompt"], "Untouched")
            self.assertNotIn("tier", result)
            self.assertNotIn("mode", result)
            self.assertEqual(body, original)
        self.assertEqual(self.path.read_bytes(), before)

    def test_default_never_falls_back_when_disabled_expired_or_wrong_mode(self):
        self.add(OTHER_STANDARD, "ref")
        with self.assertRaisesRegex(QualitySelectionError, "quality_tier_unavailable"):
            self.resolve()
        value = self.add(STANDARD, "ref")
        for path, replacement in (
            (("enabled",), False),
            (("qualification", "expires_at"), self.now + 1),
            (("reservation", "expires_at"), self.now + 1),
            (("qualification", "verified_at"), self.now + 1),
        ):
            original = copy.deepcopy(value)
            target = value
            for key in path[:-1]:
                target = target[key]
            target[path[-1]] = replacement
            self.write()
            with self.subTest(path=path):
                with self.assertRaisesRegex(
                    QualitySelectionError, "quality_tier_unavailable"
                ):
                    self.resolve()
            value.clear()
            value.update(original)
        self.write()
        with self.assertRaisesRegex(QualitySelectionError, "quality_tier_unavailable"):
            self.resolve(mode="fl")

    def test_nonexpiring_binding_is_current_without_extending_any_finite_bound(self):
        from studio_platform.execution_profiles import public_profiles
        from unittest.mock import patch

        value = self.add(STANDARD, "ref")
        value["qualification"]["expires_at"] = None
        value["reservation"]["expires_at"] = None
        self.write()
        before = self.path.read_bytes()
        self.assertEqual(self.resolve()["deployment_profile_id"], STANDARD)
        with patch("time.time", return_value=self.now):
            profile = next(
                p for p in public_profiles(self.settings) if p["id"] == STANDARD
            )
            self.assertTrue(profile["generation_support"]["ref"]["enabled"])
        self.assertEqual(self.path.read_bytes(), before)
        value["reservation"]["expires_at"] = self.now + 1
        self.write()
        with self.assertRaisesRegex(QualitySelectionError, "quality_tier_unavailable"):
            self.resolve()

    def test_exact_one_binding_or_ambiguity_and_fast_has_no_fallback(self):
        self.settings = replace(self.settings, default_deployment_profile_id=None)
        self.add(STANDARD, "ref")
        self.assertEqual(self.resolve()["deployment_profile_id"], STANDARD)
        self.add(OTHER_STANDARD, "ref")
        with self.assertRaisesRegex(QualitySelectionError, "quality_tier_ambiguous"):
            self.resolve()
        self.add(REFINE, "ref")
        self.assertEqual(self.resolve(tier="refine")["deployment_profile_id"], REFINE)
        with self.assertRaisesRegex(QualitySelectionError, "quality_tier_unavailable"):
            self.resolve(tier="fast")
        self.path.write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(QualitySelectionError, "quality_tier_unavailable"):
            self.resolve()

    def test_raw_id_authority_and_conflicts_are_safe_before_config_reads(self):
        raw = generation_request(deployment_profile_id=STANDARD)
        self.assertEqual(self.normalize(raw), (raw, None))
        for key, value in (
            ("deployment_profile_id", STANDARD),
            ("recipe_id", "private-unknown-recipe"),
            ("model_id", "private-model"),
        ):
            with self.subTest(key=key):
                with self.assertRaises(QualitySelectionError) as caught:
                    self.normalize({key: value}, allow_internal=False)
                self.assertEqual(caught.exception.status, 403)
                self.assertEqual(
                    caught.exception.code, "model_selection_admin_required"
                )
                self.assertNotIn(value, caught.exception.detail)
        conflicts = (
            {"tier": "standard", "mode": "fl", "recipe_id": "video-ref-v1"},
            {"tier": "standard", "deployment_profile_id": "quality:refine"},
            {"tier": "standard", "mode": "ref", "deployment_profile_id": STANDARD},
            {"tier": "standard", "mode": "ref", "recipe_id": "private-unknown-recipe"},
        )
        for body in conflicts:
            with self.subTest(body=body):
                with self.assertRaisesRegex(
                    QualitySelectionError, "quality_selection_conflict"
                ):
                    self.normalize(body, allow_internal=True)
        for body in (
            {"tier": "other", "mode": "ref"},
            {"tier": "standard", "mode": []},
            {"deployment_profile_id": "quality:other", "mode": "ref"},
            {"tier": "standard", "recipe_id": "video-other-v1"},
        ):
            with self.assertRaises(QualitySelectionError) as caught:
                self.normalize(body, allow_internal=False)
            self.assertEqual(caught.exception.status, 422)

    def test_legacy_mode_only_uses_server_default_without_quality_fallback(self):
        body = {"recipe_id": MODE_RECIPES["ref"], "prompt": "Keep me"}
        # Configured defaults MUST validate their requested mode, even for
        # deprecated mode-only input; missing stock is not a routing signal.
        with self.assertRaisesRegex(QualitySelectionError, "quality_tier_unavailable"):
            self.normalize(body, allow_internal=False)
        self.add(STANDARD, "ref")
        selected, frozen = self.normalize(body, allow_internal=False)
        self.assertEqual(selected["deployment_profile_id"], STANDARD)
        self.assertEqual(frozen["tier"], "standard")
        self.assertEqual(selected["prompt"], body["prompt"])
        self.settings = replace(self.settings, default_deployment_profile_id=None)
        self.assertEqual(self.normalize(body, allow_internal=False), (body, None))
        self.assertEqual(
            self.normalize({"mode": "ref", "prompt": "Keep me"}, allow_internal=False),
            (body, None),
        )
        with self.assertRaisesRegex(QualitySelectionError, "quality_tier_unavailable"):
            self.normalize({"tier": "fast", "mode": "ref"}, allow_internal=False)
        for key in ("model", "model_id", "deployment_profile_id"):
            with (
                self.subTest(key=key),
                self.assertRaisesRegex(
                    QualitySelectionError, "model_selection_admin_required"
                ),
            ):
                self.normalize({**body, key: "private-selection"}, allow_internal=False)

    def test_alias_normalization_compiles_exact_profile_without_changing_helpers(self):
        self.add(STANDARD, "ref")
        body = generation_request(
            deployment_profile_id=profile_alias("standard"),
            recipe_id=recipe_alias("ref"),
            inputs={"images": ["owned-image"]},
            controls={"duration": 5, "resolution": "480P", "steps": 20, "seed": "42"},
        )
        image = {
            "metadata": {
                "kind": "image",
                "width": 832,
                "height": 480,
                "model_ready": True,
            }
        }
        normalized, selection = self.normalize(body, allow_internal=False)
        compiled, original_hash = compile_request(
            normalized, lambda _: image, backend="wangp-worker"
        )
        self.assertEqual(compiled["deployment_profile_id"], STANDARD)
        self.assertEqual(compiled["request"]["mode"], "ref")
        self.assertEqual(selection["tier"], "standard")
        direct, direct_hash = compile_request(
            generation_request(
                deployment_profile_id=STANDARD,
                recipe_id=MODE_RECIPES["ref"],
                inputs={"images": ["owned-image"]},
                controls=normalized["controls"],
            ),
            lambda _: image,
            backend="wangp-worker",
        )
        self.assertEqual((compiled, original_hash), (direct, direct_hash))
        for mode, recipe in MODE_RECIPES.items():
            self.assertEqual(recipe_mode(recipe), mode)
            self.assertEqual(recipe_mode(recipe_alias(mode)), mode)
        self.assertIsNone(recipe_mode("unknown"))


if __name__ == "__main__":
    unittest.main()
