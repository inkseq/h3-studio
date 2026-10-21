"""Synthetic policies and locally registered fake slots, never a GPU/provider call."""

import copy
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from studio_platform.api import create_app
from studio_platform.capabilities import MODEL, compile_request
from studio_platform.control import WorkerControl, WorkerSpec
from studio_platform.execution_policy import (
    ExecutionPolicies,
    authority_current,
    authority_deadline,
    read_policy,
    validate_policy,
)
from studio_platform.qualification_profiles import (
    MULTIMODAL_PROFILE,
    MULTIMODAL_INPUT_LIMITS,
)
from studio_platform.repository import Repository, Scope, Conflict, BudgetExceeded
from studio_platform.settings import Settings
from test_platform_api import project, generation_request


def policy(now):
    return {
        "id": "self-hosted-default",
        "revision": "synthetic-v1",
        "enabled": True,
        "model_id": MODEL,
        "backend": "comfy-worker",
        "pool": "synthetic-pool",
        "configuration_id": "synthetic-config",
        "recipe_ids": ["h3-base-fl2va-v1", "h3-base-ref2va-v1"],
        "qualification": {
            "status": "accepted",
            "evidence_id": "synthetic-test-only",
            "verified_at": now - 1,
            "expires_at": now + 3600,
        },
        "envelope": {
            "max_pixels": 768 * 1344,
            "max_duration_seconds": 16,
            "max_steps": 50,
            "max_reference_files": 12,
            "max_guides": 8,
            "allow_first_last": True,
            "allow_audio": True,
            "controls": {
                "sampler_name": ["res_multistep"],
                "scheduler": ["auto"],
                "video_decode": ["normal", "tiled"],
                "audio_decode": ["normal"],
                "encoder_device": ["default", "cpu"],
                "ref_image_size": ["max", "match"],
            },
        },
        "reservation": {
            "cost_microusd": 500000,
            "expected_runtime_s": 300,
            "expires_at": now + 3600,
            "source_id": "synthetic-quote",
        },
        "budget_accounts": [
            "test-tenant:{tenant_id}",
            "test-owner:{tenant_id}:{owner_id}",
        ],
    }


def synthetic_input_limits():
    return copy.deepcopy(MULTIMODAL_INPUT_LIMITS)


def synthetic_assets():
    # Shape-compatible inspected metadata only; no media/GPU performance claim.
    return {
        "image": {"metadata": {"kind": "image", "width": 2048, "height": 2048}},
        "last": {"metadata": {"kind": "image", "width": 2048, "height": 2048}},
        "video": {
            "metadata": {
                "kind": "video",
                "width": 832,
                "height": 480,
                "duration": 107 / 24,
                "source_duration": 4.45,
                "fps": 24,
                "frame_count": 107,
                "has_audio": True,
            }
        },
        "audio": {
            "metadata": {"kind": "audio", "duration": 4.45, "source_duration": 4.45}
        },
    }


class ExecutionPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = Repository("sqlite:///" + (self.root / "test.sqlite3").as_posix())
        self.repo.create_schema()
        self.addCleanup(self.repo.close)
        self.path = self.root / "synthetic-policy.json"
        self.settings = Settings(
            self.root,
            auth_mode="local-test",
            execution_backend="comfy-worker",
            generation_enabled=True,
            execution_policy_file=self.path,
        )
        self.scope = Scope("sixnine", "superdan", "story-one")
        self.compiled, self.fingerprint = compile_request(
            generation_request(), lambda _: None
        )
        self.value = policy(self.repo.clock())
        self.write()
        self.policies = ExecutionPolicies(self.settings, self.repo)
        self.repo.configure_capacity(max_instances=1, max_physical_gpus=1)
        self.control = WorkerControl(self.repo)
        self.control.register(
            WorkerSpec(
                "synthetic-worker",
                "synthetic-pool",
                "synthetic-provider",
                "synthetic-instance",
                ("synthetic-gpu",),
                ("h3-base-fl2va-v1", "h3-base-ref2va-v1"),
                MODEL,
                "synthetic-config",
            )
        )
        self.control.mark_ready("synthetic-worker", upstream_idle_confirmed=True)
        self.repo.configure_budget(
            "test-tenant:sixnine", tenant_id="sixnine", limit_microusd=1000000
        )
        self.repo.configure_budget(
            "test-owner:sixnine:superdan",
            tenant_id="sixnine",
            owner_id="superdan",
            limit_microusd=1000000,
        )

    def write(self):
        self.path.write_text(json.dumps(self.value), encoding="utf-8")
        self.path.chmod(0o600)

    def evaluate(self):
        return self.policies.evaluate(self.compiled, self.scope, self.fingerprint)

    def test_missing_binding_has_distinct_read_only_diagnostic_then_zero_budget_refusal(
        self,
    ):
        from sqlalchemy import delete, select
        from studio_platform.repository import budget_accounts

        account_id = "test-owner:sixnine:superdan"
        with self.repo.transaction() as connection:
            connection.execute(
                delete(budget_accounts).where(budget_accounts.c.id == account_id)
            )
        for _ in range(2):
            result = self.evaluate()
            self.assertEqual(
                result.execution["budget_error_code"],
                "generation_budget_binding_missing",
            )
            self.assertFalse(result.execution["enabled"])
        with self.repo.engine.connect() as connection:
            self.assertIsNone(
                connection.execute(
                    select(budget_accounts).where(budget_accounts.c.id == account_id)
                ).first()
            )
        self.repo.configure_budget(
            account_id, tenant_id="sixnine", owner_id="superdan", limit_microusd=0
        )
        self.assertEqual(
            self.evaluate().execution["budget_error_code"], "budget_exceeded"
        )

    def test_enabled_exact_policy_preserves_controls_and_separates_cost(self):
        result = self.evaluate()
        self.assertTrue(result.execution["enabled"])
        self.assertEqual(result.cost, 500000)
        self.assertEqual(result.estimate["kind"], "budget_reservation")
        self.assertFalse(result.estimate["actual_charge_known"])
        self.assertEqual(result.execution["configuration_id"], "synthetic-config")
        self.assertEqual(self.compiled["request"]["seed"], "18446744073709551615")
        self.assertEqual(self.compiled["request"]["model"], MODEL)

    def test_capabilities_explain_live_deployment_preset_without_changing_model_defaults(
        self,
    ):
        from studio_platform.capabilities import capabilities, control_schema

        self.value["envelope"]["controls"].update(
            encoder_device=["cpu"], video_decode=["tiled"]
        )
        self.write()
        result = capabilities(self.settings)
        for recipe in result["recipes"]:
            self.assertEqual(
                recipe["deployment_preset"]["controls"],
                {"encoder_device": "cpu", "video_decode": "tiled"},
            )
            self.assertEqual(
                recipe["deployment_preset"]["applies_to"], "unset_controls_only"
            )
            self.assertEqual(recipe["controls"]["encoder_device"]["default"], "default")
            self.assertEqual(recipe["controls"]["video_decode"]["default"], "normal")
        self.assertNotIn("budget_accounts", json.dumps(result))
        self.assertEqual(control_schema()["encoder_device"]["default"], "default")
        self.assertFalse(self.evaluate().execution["enabled"])
        request = generation_request()
        request["controls"].update(
            result["recipes"][0]["deployment_preset"]["controls"]
        )
        compiled, fingerprint = compile_request(request, lambda _: None)
        self.assertTrue(
            self.policies.evaluate(compiled, self.scope, fingerprint).execution[
                "enabled"
            ]
        )
        self.value["qualification"]["expires_at"] = self.repo.clock() - 1
        self.write()
        self.assertTrue(
            all(
                "deployment_preset" not in r
                for r in capabilities(self.settings)["recipes"]
            )
        )

    def test_public_execution_scope_distinguishes_unqualified_recipe_from_model_support(
        self,
    ):
        from studio_platform.capabilities import capabilities

        self.value["recipe_ids"] = ["h3-base-fl2va-v1"]
        self.value["envelope"].update(
            max_reference_files=0, max_guides=0, allow_first_last=False
        )
        self.write()
        result = capabilities(self.settings)
        by_id = {recipe["id"]: recipe for recipe in result["recipes"]}
        live = by_id["h3-base-fl2va-v1"]["execution_support"]
        self.assertEqual(live["status"], "qualified")
        self.assertFalse(live["capacity_checked"])
        self.assertTrue(live["preflight_required"])
        self.assertEqual(live["constraints"]["max_reference_files"], 0)
        self.assertFalse(live["constraints"]["allow_first_last"])
        unsupported = by_id["h3-base-ref2va-v1"]
        self.assertTrue(unsupported["implemented"])
        self.assertEqual(unsupported["execution_support"]["status"], "not_qualified")
        self.assertNotIn("constraints", unsupported["execution_support"])
        serialized = json.dumps(result)
        for private in (
            "synthetic-pool",
            "synthetic-config",
            "test-owner",
            "budget_accounts",
            "synthetic-test-only",
        ):
            self.assertNotIn(private, serialized)

    def test_wrong_recipe_or_controls_do_not_report_unchecked_missing_capacity(self):
        self.value["recipe_ids"] = ["h3-base-fl2va-v1"]
        self.value["envelope"]["controls"].update(
            encoder_device=["cpu"], video_decode=["tiled"]
        )
        self.write()
        with (
            patch.object(
                self.control.__class__,
                "pool_status",
                side_effect=AssertionError("range must pass before capacity"),
            ),
            patch.object(
                self.repo,
                "find_capacity_approval",
                side_effect=AssertionError(
                    "no approval lookup for unsupported request"
                ),
            ),
        ):
            blocked = self.evaluate()
            text = " ".join(blocked.execution["blockers"])
            self.assertIn("编码器设备当前为 default", text)
            self.assertIn("视频 VAE 解码当前为 normal", text)
            self.assertNotIn("冷启动审批", text)
            other = copy.deepcopy(self.compiled)
            other["recipe_id"] = "h3-base-ref2va-v1"
            other["request"].update(encoder_device="cpu", video_decode="tiled")
            mismatch = self.policies.evaluate(other, self.scope, self.fingerprint)
            self.assertIn("执行池未验收此模型或配方", mismatch.execution["blockers"])
            self.assertFalse(
                any("冷启动审批" in value for value in mismatch.execution["blockers"])
            )

    def test_optional_input_limits_are_complete_strict_and_do_not_change_old_policies(
        self,
    ):
        self.assertIs(validate_policy(self.value), self.value)
        self.value["envelope"]["input_limits"] = synthetic_input_limits()
        self.assertIs(validate_policy(self.value), self.value)
        for field, value in (
            ("max_images", True),
            ("max_videos", 4),
            ("max_audio_duration_seconds", float("nan")),
            ("guide_kinds", ["image", "image"]),
            ("guide_recipe_ids", ["other-recipe"]),
            ("allow_video_audio", 1),
        ):
            broken = copy.deepcopy(self.value)
            broken["envelope"]["input_limits"][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                validate_policy(broken)
        for change in ("missing", "unknown"):
            broken = copy.deepcopy(self.value)
            if change == "missing":
                broken["envelope"]["input_limits"].pop("max_videos")
            else:
                broken["envelope"]["input_limits"]["max_video"] = 1
            with self.assertRaises(ValueError):
                validate_policy(broken)

    def test_qualified_multimodal_shape_and_two_first_last_images_pass_without_broadening_recipe(
        self,
    ):
        from studio_platform.capabilities import capabilities

        self.value["envelope"].update(
            input_limits=synthetic_input_limits(), max_reference_files=3, max_guides=1
        )
        self.write()
        assets = synthetic_assets()
        for recipe, inputs in (
            (
                "h3-base-ref2va-v1",
                {
                    "images": ["image"],
                    "videos": ["video"],
                    "audios": ["audio"],
                    "guides": [{"media_id": "image", "time_seconds": 1}],
                },
            ),
            ("h3-base-fl2va-v1", {"first_frame": "image", "last_frame": "last"}),
        ):
            compiled, fingerprint = compile_request(
                generation_request(recipe_id=recipe, inputs=inputs), assets.__getitem__
            )
            result = self.policies.evaluate(compiled, self.scope, fingerprint)
            self.assertTrue(result.execution["enabled"], result.execution["blockers"])
        published = capabilities(self.settings)["recipes"][0]["execution_support"][
            "constraints"
        ]
        self.assertEqual(published["input_limits"], synthetic_input_limits())

    def test_aggregate_three_assets_does_not_admit_three_videos_or_unverified_metadata(
        self,
    ):
        self.value["envelope"].update(
            input_limits=synthetic_input_limits(), max_reference_files=3, max_guides=1
        )
        self.write()
        assets = synthetic_assets()
        assets["video-two"] = copy.deepcopy(assets["video"])
        assets["video-three"] = copy.deepcopy(assets["video"])
        compiled, fingerprint = compile_request(
            generation_request(
                recipe_id="h3-base-ref2va-v1",
                inputs={"videos": ["video", "video-two", "video-three"]},
            ),
            assets.__getitem__,
        )
        with patch.object(
            self.control.__class__,
            "pool_status",
            side_effect=AssertionError("must reject before capacity"),
        ):
            result = self.policies.evaluate(compiled, self.scope, fingerprint)
        self.assertIn("3 份视频参考", " ".join(result.execution["blockers"]))
        self.assertIn("总时长", " ".join(result.execution["blockers"]))
        self.assertNotIn("冷启动审批", " ".join(result.execution["blockers"]))
        compiled, fingerprint = compile_request(
            generation_request(inputs={"first_frame": "image"}), assets.__getitem__
        )
        compiled["assets"]["image"]["metadata"].pop("width")
        self.assertIn(
            "已核验的尺寸",
            " ".join(
                self.policies.evaluate(compiled, self.scope, fingerprint).execution[
                    "blockers"
                ]
            ),
        )

    def test_per_kind_bounds_cover_source_normalized_pixels_and_guide_recipe_without_mutation(
        self,
    ):
        self.value["envelope"].update(
            input_limits=synthetic_input_limits(), max_reference_files=3, max_guides=1
        )
        self.write()
        assets = synthetic_assets()
        compiled, fingerprint = compile_request(
            generation_request(
                recipe_id="h3-base-ref2va-v1",
                inputs={"videos": ["video"], "audios": ["audio"], "images": ["image"]},
            ),
            assets.__getitem__,
        )
        changes = [
            ("video", "width", 1024, "像素上限"),
            ("image", "height", 4096, "含首尾帧"),
            ("video", "source_duration", 4.6, "总时长"),
            ("video", "duration", 4.6, "总时长"),
            ("audio", "duration", 4.46, "总时长"),
            ("audio", "source_duration", None, "已核验的时长"),
        ]
        for asset, field, value, expected in changes:
            changed = copy.deepcopy(compiled)
            changed["assets"][asset]["metadata"][field] = value
            snapshot = copy.deepcopy(changed)
            blocked = self.policies.evaluate(changed, self.scope, fingerprint)
            self.assertIn(expected, " ".join(blocked.execution["blockers"]))
            self.assertEqual(snapshot, changed)
        compiled, fingerprint = compile_request(
            generation_request(
                inputs={
                    "first_frame": "image",
                    "last_frame": "last",
                    "guides": [{"media_id": "image", "time_seconds": 1}],
                }
            ),
            assets.__getitem__,
        )
        self.assertIn(
            "当前生成方式尚未开放时间锚点",
            " ".join(
                self.policies.evaluate(compiled, self.scope, fingerprint).execution[
                    "blockers"
                ]
            ),
        )

    def test_guide_kind_and_video_audio_consent_are_checked_independently(self):
        limits = synthetic_input_limits()
        limits["allow_video_audio"] = False
        self.value["envelope"].update(
            input_limits=limits, max_reference_files=3, max_guides=1
        )
        self.write()
        assets = synthetic_assets()
        body = generation_request(
            recipe_id="h3-base-ref2va-v1",
            inputs={
                "videos": ["video"],
                "guides": [{"media_id": "video", "time_seconds": 0, "use_audio": True}],
            },
        )
        compiled, fingerprint = compile_request(body, assets.__getitem__)
        blocked = self.policies.evaluate(compiled, self.scope, fingerprint)
        self.assertIn("时间锚点类型", " ".join(blocked.execution["blockers"]))
        self.assertIn("参考视频原声", " ".join(blocked.execution["blockers"]))
        body["inputs"] = {"videos": [{"asset_id": "video", "include_audio": False}]}
        compiled, fingerprint = compile_request(body, assets.__getitem__)
        self.assertTrue(
            self.policies.evaluate(compiled, self.scope, fingerprint).execution[
                "enabled"
            ]
        )

    def test_runtime_required_is_bound_to_explicit_new_suite_and_never_reported_as_verified(
        self,
    ):
        from studio_platform.capabilities import capabilities
        from studio_platform.repository import request_hash

        self.value["qualification"].update(
            status="runtime_required", profile=MULTIMODAL_PROFILE
        )
        self.value["envelope"]["input_limits"] = synthetic_input_limits()
        self.write()
        result = capabilities(self.settings)
        for recipe in result["recipes"]:
            support = recipe["execution_support"]
            self.assertEqual(support["status"], "runtime_required")
            self.assertTrue(support["runtime_verification_required"])
            self.assertFalse(support["capacity_checked"])
            self.assertIn("GPU启动后先验证", support["reason"])
            self.assertEqual(
                support["constraints"]["input_limits"]["max_image_pixels"], 2048 * 2048
            )
        # A synthetic already-ready slot is the only capacity in this unit test;
        # the production boot suite owns registration after real inference.
        admitted = self.evaluate()
        self.assertTrue(admitted.execution["enabled"])
        plan = self.repo.create_plan(
            self.scope,
            self.compiled,
            admitted.execution,
            expires_at=admitted.expires_at,
            estimated_cost_microusd=admitted.cost,
        )
        job = self.repo.create_job(
            self.scope,
            plan["id"],
            "runtime-authorized",
            budget_account_ids=admitted.execution["budget_account_ids"],
        )
        self.assertTrue(self.policies.activation_allowed(job))
        approval = dict(
            policy_hash=request_hash(self.value),
            model_id=self.value["model_id"],
            pool=self.value["pool"],
            configuration_id=self.value["configuration_id"],
            recipe_ids=self.value["recipe_ids"],
            qualification_evidence_id=self.value["qualification"]["evidence_id"],
            qualification_expires_at=self.value["qualification"]["expires_at"],
            quote_expires_at=self.value["reservation"]["expires_at"],
            expires_at=self.repo.clock() + 100,
        )
        self.assertTrue(self.policies.capacity_approval_current(approval))
        self.value["qualification"]["status"] = "unverified"
        self.write()
        self.assertFalse(self.policies.activation_allowed(job))
        self.assertFalse(self.policies.capacity_approval_current(approval))

    def test_runtime_required_without_explicit_suite_or_beyond_its_bounds_fails_closed(
        self,
    ):
        baseline = copy.deepcopy(self.value)
        baseline["qualification"].update(
            status="runtime_required", profile=MULTIMODAL_PROFILE
        )
        baseline["envelope"]["input_limits"] = synthetic_input_limits()
        for change in (
            "missing-profile",
            "unknown-profile",
            "legacy-profile",
            "missing-limits",
            "oversized-image",
            "fl-guide",
        ):
            broken = copy.deepcopy(baseline)
            if change == "missing-profile":
                broken["qualification"].pop("profile")
            elif change == "unknown-profile":
                broken["qualification"]["profile"] = "unqualified-runtime"
            elif change == "legacy-profile":
                broken["qualification"]["profile"] = "fl50"
            elif change == "missing-limits":
                broken["envelope"].pop("input_limits")
            elif change == "oversized-image":
                broken["envelope"]["input_limits"]["max_image_pixels"] = 2048 * 2048 + 1
            else:
                broken["envelope"]["input_limits"]["guide_recipe_ids"].append(
                    "h3-base-fl2va-v1"
                )
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate_policy(broken)

    def test_missing_invalid_or_disabled_policy_never_falls_back(self):
        self.path.unlink()
        self.assertFalse(self.evaluate().execution["enabled"])
        self.path.write_text('{"not-a-policy":"invalid"}', encoding="utf-8")
        self.assertFalse(self.evaluate().execution["enabled"])
        self.value["enabled"] = False
        self.write()
        self.assertFalse(self.evaluate().execution["enabled"])
        with self.assertRaises(ValueError):
            read_policy(Path("relative.json"))

    def test_qualification_expiry_budget_scope_and_envelope_block(self):
        original = copy.deepcopy(self.value)
        modifications = [
            ("qualification", "status", "unverified"),
            ("qualification", "expires_at", self.repo.clock() - 0.1),
            ("reservation", "expires_at", self.repo.clock() - 1),
            ("envelope", "max_duration_seconds", 2),
            ("envelope", "max_pixels", 65536),
            ("envelope", "max_steps", 1),
            ("envelope", "allow_audio", False),
        ]
        for section, key, val in modifications:
            self.value = copy.deepcopy(original)
            self.value[section][key] = val
            self.write()
            with self.subTest(section=section, key=key):
                self.assertFalse(self.evaluate().execution["enabled"])
        self.value = original
        self.value["budget_accounts"] = ["test-owner:sixnine:supervan"]
        self.repo.configure_budget(
            "test-owner:sixnine:supervan",
            tenant_id="sixnine",
            owner_id="supervan",
            limit_microusd=1000000,
        )
        self.write()
        self.assertFalse(self.evaluate().execution["enabled"])

    def test_healthy_busy_slot_accepts_queue_but_unknown_does_not(self):
        admission = self.evaluate()
        plan = self.repo.create_plan(
            self.scope,
            self.compiled,
            admission.execution,
            expires_at=admission.expires_at,
            estimated_cost_microusd=admission.cost,
        )
        self.repo.create_job(
            self.scope,
            plan["id"],
            "one",
            budget_account_ids=admission.execution["budget_account_ids"],
        )
        claim = self.control.claim("synthetic-worker", "synthetic-pool")
        self.assertIsNotNone(claim)
        self.assertTrue(self.evaluate().execution["enabled"])
        from sqlalchemy import update
        from studio_platform.repository import registered_workers

        with self.repo.transaction() as conn:
            conn.execute(
                update(registered_workers).values(expires_at=self.repo.clock() - 1)
            )
        self.assertFalse(self.evaluate().execution["enabled"])

    def test_changed_policy_needs_new_consent_but_identical_plan_is_current(self):
        result = self.evaluate()
        plan = self.repo.create_plan(
            self.scope,
            self.compiled,
            result.execution,
            expires_at=result.expires_at,
            estimated_cost_microusd=result.cost,
        )
        self.assertEqual(len(self.policies.ensure_current(plan, self.scope)), 2)
        self.value["reservation"]["cost_microusd"] = 600000
        self.write()
        with self.assertRaises(Conflict):
            self.policies.ensure_current(plan, self.scope)

    def test_both_owners_share_warm_pool_across_stories_but_unfunded_owner_is_blocked(
        self,
    ):
        self.repo.configure_budget(
            "test-owner:sixnine:supervan",
            tenant_id="sixnine",
            owner_id="supervan",
            limit_microusd=1000000,
        )
        for owner in ("superdan", "supervan"):
            for story in ("existing-story", "new-story"):
                admission = self.policies.evaluate(
                    self.compiled, Scope("sixnine", owner, story), self.fingerprint
                )
                self.assertTrue(
                    admission.execution["enabled"], admission.execution["blockers"]
                )
                self.assertEqual(admission.execution["admission_state"], "queued")
        denied = self.policies.evaluate(
            self.compiled,
            Scope("sixnine", "unfunded-user", "new-story"),
            self.fingerprint,
        )
        self.assertFalse(denied.execution["enabled"])

    def test_exhaustion_reports_clear_reason_and_rejects_new_submit_without_hiding_replay(
        self,
    ):
        app = create_app(self.settings, repository=self.repo)
        with TestClient(app) as client:
            client.post("/api/auth/login", json={"username": "superdan"})
            client.post("/v1/projects", json={"project": project()}).raise_for_status()
            plan = client.post("/v1/generation-plans", json=generation_request()).json()
            original = client.post(
                "/v1/jobs",
                json={"plan_id": plan["plan_id"]},
                headers={"Idempotency-Key": "before-exhaustion"},
            )
            original.raise_for_status()
            self.repo.configure_budget(
                "test-owner:sixnine:superdan",
                tenant_id="sixnine",
                owner_id="superdan",
                limit_microusd=500000,
            )
            denied = self.evaluate()
            self.assertEqual(denied.execution["budget_error_code"], "budget_exceeded")
            self.assertIn("额度不足", " ".join(denied.execution["blockers"]))
            retry = client.post(
                "/v1/jobs",
                json={"plan_id": plan["plan_id"]},
                headers={"Idempotency-Key": "before-exhaustion"},
            )
            self.assertEqual(retry.json()["id"], original.json()["id"])
            fresh = client.post(
                "/v1/jobs",
                json={"plan_id": plan["plan_id"]},
                headers={"Idempotency-Key": "after-exhaustion"},
            )
            self.assertEqual(fresh.status_code, 429, fresh.text)
            self.assertEqual(fresh.json()["code"], "budget_exceeded")
            self.assertIn("额度不足", fresh.json()["detail"])
            blocked_plan = client.post(
                "/v1/generation-plans", json=generation_request()
            ).json()
            denied_submit = client.post(
                "/v1/jobs",
                json={"plan_id": blocked_plan["plan_id"]},
                headers={"Idempotency-Key": "blocked-original-plan"},
            )
            self.assertEqual(denied_submit.status_code, 429, denied_submit.text)
            self.assertEqual(
                len(self.repo.list_jobs_for_owner("sixnine", "superdan")), 1
            )

    def test_near_expiry_blocks_new_work_without_changing_policy_or_existing_reservation(
        self,
    ):
        now = self.repo.clock()
        self.repo.clock = lambda: now
        self.value["qualification"]["expires_at"] = now + 700
        self.value["reservation"]["expires_at"] = now + 1000
        self.write()
        admission = self.evaluate()
        self.assertTrue(admission.execution["enabled"])
        self.assertEqual(admission.expires_at, now + 400)
        plan = self.repo.create_plan(
            self.scope,
            self.compiled,
            admission.execution,
            expires_at=admission.expires_at,
            estimated_cost_microusd=admission.cost,
        )
        job = self.repo.create_job(
            self.scope,
            plan["id"],
            "before-service-expiry",
            budget_account_ids=admission.execution["budget_account_ids"],
        )
        original_hash = admission.execution["policy_hash"]
        self.repo.clock = lambda: now + 400
        late = self.evaluate()
        self.assertEqual(late.execution["policy_hash"], original_hash)
        self.assertFalse(late.execution["enabled"])
        self.assertTrue(
            any("剩余时间不足" in item for item in late.execution["blockers"])
        )
        self.assertFalse(self.policies.submission_allowed(job))
        with self.assertRaises(Conflict):
            self.policies.ensure_current(plan, self.scope)
        self.assertEqual(
            self.repo.get_budget("test-owner:sixnine:superdan")["reserved_microusd"],
            500000,
        )

    def test_explicit_nonexpiring_authority_preserves_finite_plan_and_original_hash(
        self,
    ):
        from studio_platform.capabilities import capabilities
        from studio_platform.repository import request_hash

        now = self.repo.clock()
        self.repo.clock = lambda: now
        for section in ("qualification", "reservation"):
            self.value[section]["expires_at"] = None
        original = copy.deepcopy(self.value)
        self.write()
        admitted = self.evaluate()
        self.assertTrue(admitted.execution["enabled"], admitted.execution)
        self.assertEqual(admitted.expires_at, now + 900)
        self.assertEqual(admitted.execution["policy_hash"], request_hash(original))
        self.assertIsNone(admitted.execution["qualification_expires_at"])
        self.assertIsNone(admitted.execution["quote_expires_at"])
        self.assertEqual(self.value, original)
        self.assertIsNone(authority_deadline(self.value))
        plan = self.repo.create_plan(
            self.scope,
            self.compiled,
            admitted.execution,
            expires_at=admitted.expires_at,
            estimated_cost_microusd=admitted.cost,
        )
        job = self.repo.create_job(
            self.scope,
            plan["id"],
            "nonexpiring-authority",
            budget_account_ids=admitted.execution["budget_account_ids"],
        )
        # No artificial renewal is needed for already admitted authority, but
        # new confirmations still use the original finite plan lease.
        self.repo.clock = lambda: now + 86400
        self.assertTrue(self.policies.activation_allowed(job))
        for recipe in capabilities(self.settings)["recipes"]:
            self.assertIsNone(recipe["execution_support"]["expires_at"])
        with self.assertRaisesRegex(Conflict, "plan_expired"):
            self.repo.create_job(
                self.scope,
                plan["id"],
                "expired-confirmation",
                budget_account_ids=admitted.execution["budget_account_ids"],
            )

    def test_nonexpiring_authority_keeps_any_remaining_finite_bound(self):
        now = self.repo.clock()
        self.repo.clock = lambda: now
        for section in ("qualification", "reservation"):
            other = "reservation" if section == "qualification" else "qualification"
            value = copy.deepcopy(self.value)
            value[section]["expires_at"] = None
            value[other]["expires_at"] = now + 301
            self.value = value
            self.write()
            with self.subTest(finite=other):
                self.assertEqual(authority_deadline(value), now + 301)
                self.assertTrue(authority_current(value, now, 300))
                self.assertFalse(authority_current(value, now + 1, 300))
                self.assertEqual(self.evaluate().expires_at, now + 1)
                self.repo.clock = lambda: now + 1
                self.assertFalse(self.evaluate().execution["enabled"])
                self.repo.clock = lambda: now

    def test_nonexpiring_is_explicit_not_missing_or_invalid_authority(self):
        original = copy.deepcopy(self.value)
        for section in ("qualification", "reservation"):
            for value in (False, 0, -1, float("inf"), "never"):
                invalid = copy.deepcopy(original)
                invalid[section]["expires_at"] = value
                with (
                    self.subTest(section=section, value=value),
                    self.assertRaises(ValueError),
                ):
                    validate_policy(invalid)
            invalid = copy.deepcopy(original)
            del invalid[section]["expires_at"]
            with (
                self.subTest(section=section, value="missing"),
                self.assertRaises(ValueError),
            ):
                validate_policy(invalid)

    def test_demand_authority_admits_one_original_job_without_claiming_ready_capacity(
        self,
    ):
        from studio_platform.repository import request_hash

        self.control.retire("synthetic-worker", upstream_idle_confirmed=True)
        marker = {
            "version": 1,
            "profile_id": "protected-profile",
            "profile_hash": "a" * 64,
            "policy_hash": request_hash(self.value),
            "test_task_id": "test-task",
        }
        with patch(
            "studio_platform.dstack_demand.demand_admission", return_value=marker
        ) as demand:
            admitted = self.evaluate()
            self.assertTrue(admitted.execution["enabled"], admitted.execution)
            self.assertEqual(admitted.execution["admission_state"], "queued")
            self.assertEqual(admitted.execution["registered_healthy_slots"], 0)
            self.assertEqual(admitted.execution["demand_start"], marker)
            self.assertNotIn("capacity_approval_id", admitted.execution)
            demand.assert_called_once_with(
                self.settings, self.repo, self.scope, self.compiled, self.value
            )
            plan = self.repo.create_plan(
                self.scope,
                self.compiled,
                admitted.execution,
                expires_at=admitted.expires_at,
                estimated_cost_microusd=admitted.cost,
            )
            self.policies.ensure_current(plan, self.scope)
            self.control.register(
                WorkerSpec(
                    "new-warm-worker",
                    self.value["pool"],
                    "synthetic-provider",
                    "new-warm-instance",
                    ("new-warm-gpu",),
                    tuple(self.value["recipe_ids"]),
                    self.value["model_id"],
                    self.value["configuration_id"],
                )
            )
            self.control.mark_ready("new-warm-worker", upstream_idle_confirmed=True)
            # Readiness can arrive between preview and confirmation without
            # changing its original exact automatic-start authority marker.
            self.policies.ensure_current(plan, self.scope)
            job = self.repo.create_job(
                self.scope,
                plan["id"],
                "confirmed-demand",
                budget_account_ids=admitted.execution["budget_account_ids"],
            )
        with patch("studio_platform.dstack_demand.demand_admission", return_value=None):
            self.assertTrue(self.evaluate().execution["enabled"])
            with self.assertRaisesRegex(Conflict, "execution_policy_changed"):
                self.policies.ensure_current(plan, self.scope)
            replay = self.repo.create_job(
                self.scope,
                plan["id"],
                "confirmed-demand",
                budget_account_ids=admitted.execution["budget_account_ids"],
            )
            self.assertEqual(replay["id"], job["id"])
            self.assertTrue(self.policies.activation_allowed(job))
        self.assertEqual(job["status"], "queued")
        self.assertEqual(
            self.repo.get_budget("test-owner:sixnine:superdan")["reserved_microusd"],
            500000,
        )

    def test_demand_authority_does_not_bypass_policy_or_budget_blockers(self):
        self.control.retire("synthetic-worker", upstream_idle_confirmed=True)
        self.value["enabled"] = False
        self.write()
        with patch("studio_platform.dstack_demand.demand_admission") as demand:
            self.assertFalse(self.evaluate().execution["enabled"])
            demand.assert_not_called()
        self.value["enabled"] = True
        self.write()
        self.repo.configure_budget(
            "test-owner:sixnine:superdan",
            tenant_id="sixnine",
            owner_id="superdan",
            limit_microusd=1,
        )
        with patch("studio_platform.dstack_demand.demand_admission") as demand:
            self.assertFalse(self.evaluate().execution["enabled"])
            demand.assert_not_called()

    def test_submission_guard_does_not_reserve_twice_and_honors_revocation(self):
        admission = self.evaluate()
        plan = self.repo.create_plan(
            self.scope,
            self.compiled,
            admission.execution,
            expires_at=admission.expires_at,
            estimated_cost_microusd=admission.cost,
        )
        job = self.repo.create_job(
            self.scope,
            plan["id"],
            "one",
            budget_account_ids=admission.execution["budget_account_ids"],
        )
        self.repo.configure_budget(
            "test-tenant:sixnine", tenant_id="sixnine", limit_microusd=500000
        )
        self.assertTrue(self.policies.submission_allowed(job))
        self.assertEqual(
            self.repo.get_budget("test-tenant:sixnine")["reserved_microusd"], 500000
        )
        self.value["enabled"] = False
        self.write()
        self.assertFalse(self.policies.submission_allowed(job))

    def test_real_api_reserves_budgets_and_idempotent_retry_survives_revocation(self):
        app = create_app(self.settings, repository=self.repo)
        with TestClient(app) as client:
            client.post("/api/auth/login", json={"username": "superdan"})
            self.assertEqual(
                client.post("/v1/projects", json={"project": project()}).status_code,
                201,
            )
            plan = client.post("/v1/generation-plans", json=generation_request()).json()
            self.assertEqual(plan["status"], "ready")
            response = client.post(
                "/v1/jobs",
                json={"plan_id": plan["plan_id"]},
                headers={"Idempotency-Key": "one"},
            )
            self.assertEqual(response.status_code, 202, response.text)
            self.assertEqual(response.json()["status"], "queued")
            self.assertFalse(response.json()["simulation"])
            for account_id in ("test-tenant:sixnine", "test-owner:sixnine:superdan"):
                self.assertEqual(
                    self.repo.get_budget(account_id)["reserved_microusd"], 500000
                )
            self.value["enabled"] = False
            self.write()
            retry = client.post(
                "/v1/jobs",
                json={"plan_id": plan["plan_id"]},
                headers={"Idempotency-Key": "one"},
            )
            self.assertEqual(retry.status_code, 202)
            self.assertEqual(retry.json()["id"], response.json()["id"])
            self.assertEqual(
                client.post(
                    "/v1/jobs",
                    json={"plan_id": plan["plan_id"]},
                    headers={"Idempotency-Key": "new-attempt"},
                ).status_code,
                409,
            )

    def test_batch_admission_reserves_same_budgets_only_once(self):
        app = create_app(self.settings, repository=self.repo)
        with TestClient(app) as client:
            client.post("/api/auth/login", json={"username": "superdan"})
            client.post("/v1/projects", json={"project": project()})
            ids = [
                client.post("/v1/generation-plans", json=generation_request()).json()[
                    "plan_id"
                ]
                for _ in range(3)
            ]
            body = {"client_project_id": "story-one", "plan_ids": ids}
            response = client.post(
                "/v1/batches", json=body, headers={"Idempotency-Key": "batch"}
            )
            self.assertEqual(response.status_code, 202, response.text)
            self.assertEqual(response.json()["counts"], {"queued": 2, "rejected": 1})
            retry = client.post(
                "/v1/batches", json=body, headers={"Idempotency-Key": "batch"}
            )
            self.assertEqual(retry.status_code, 202, retry.text)
            self.assertEqual(retry.json()["id"], response.json()["id"])
            self.assertEqual(
                self.repo.get_budget("test-tenant:sixnine")["reserved_microusd"],
                1000000,
            )

    def test_config_schema_cannot_broaden_or_select_unknown_fields(self):
        for field in ("backend", "model_id", "configuration_id"):
            previous = self.value[field]
            self.value[field] = "" if field == "configuration_id" else "unknown"
            self.write()
            with self.assertRaises(ValueError):
                read_policy(self.path)
            self.value[field] = previous
        self.value["budget_accounts"] = ["{owner_id.__class__}"]
        self.write()
        with self.assertRaises(ValueError):
            read_policy(self.path)


if __name__ == "__main__":
    unittest.main()
