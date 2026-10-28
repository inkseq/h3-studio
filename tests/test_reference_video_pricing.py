"""Compiled duration billing and cross-pool owner exclusion; no providers."""

import copy
import unittest
from unittest.mock import patch

from sqlalchemy import select

from comfy_workflow import native_output_spec
from studio_platform.credits_pricing import (
    DEFAULT_CONFIGURATION,
    DEFAULT_RATES,
    PricingError,
    configuration,
    public_prices,
    public_quote,
)
from studio_platform.inference.wangp_full_compiler import normalize_request
from studio_platform.queue import TaskQueue
from studio_platform.reference_video_pricing import FIELD, RECOMMENDED, used_frames
from studio_platform.repository import Scope, attempts
from studio_platform.runtime_catalog import get_profile
import test_credits_routes as fixtures
from test_platform_repository import LedgerCase
from test_platform_wangp_full_compiler import case, meta

PROFILE = "h3-pruned-rank8-int8-sdpa-p4-29gib-taomate5-v7"


def compiled(
    *,
    sharp=False,
    video_frames=(124,),
    conditioning="reference",
    excerpts=None,
    portrait=False,
    duration=5,
):
    ids = ["v" + str(i) for i in range(len(video_frames))]
    request, metadata = case(
        images=["i"], videos=ids, resolution="768P" if sharp else "480P"
    )
    request.update(
        model=get_profile(PROFILE)["model_id"],
        steps=5,
        video_conditioning=conditioning,
        duration=duration,
    )
    if portrait:
        request["aspect_ratio"] = "9:16"
    output = native_output_spec(request)
    for identity, frames in zip(ids, video_frames, strict=True):
        metadata[identity] = meta(
            "video",
            frame_count=frames,
            duration=frames / 24,
            source_duration=min(15, frames / 24),
            width=output["width"],
            height=output["height"],
        )
    if excerpts:
        request["video_excerpts"] = excerpts
    request = normalize_request(request, metadata, output, PROFILE)
    return {
        "deployment_profile_id": PROFILE,
        "recipe_id": "h3-base-ref2va-v1",
        "request": request,
        "output_spec": output,
        "assets": {key: {"metadata": value} for key, value in metadata.items()},
    }


class ReferencePricingTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.CreditsRouteTests()
        self.f.setUp()
        self.addCleanup(self.f.tearDown)
        self.revision = self.f.enable()

    def adopt(self):
        self.body = {
            "expected_revision": self.revision,
            "rates": dict(DEFAULT_RATES),
            "resolution_multipliers": {"480P": 1, "768P": 2},
            FIELD: dict(RECOMMENDED),
            "reason": "Disposable reference pricing test",
        }
        return self.f.service.publish(self.f.admin, self.body, key="reference-price")

    def test_optional_policy_immutable_original_quote_and_audited_cas(self):
        request = compiled(sharp=True)
        original = self.f.get_quote(request)
        self.assertEqual(original["credits"], 21)
        saved = self.adopt()
        self.assertEqual(
            self.f.service.publish(self.f.admin, self.body, key="reference-price"),
            saved,
        )
        self.assertEqual(self.f.get_quote(request)["credits"], 95)
        self.assertEqual(public_quote({"credit_quote": original})["credits"], 21)
        self.assertNotIn("reference_video_frames", original)
        with self.f.repo.engine.connect() as c:
            self.assertEqual(public_prices(c, "sixnine")[FIELD], RECOMMENDED)
        with self.assertRaisesRegex(PricingError, "credit_prices_changed"):
            self.f.service.publish(self.f.admin, self.body, key="stale-reference-price")

    def test_exact_single_rounding_base_and_surcharge_reconcile(self):
        self.adopt()
        for sharp, refs, amount, base, ref_frames in (
            (False, (), 21, 21, 0),
            (True, (), 42, 42, 0),
            (False, (124,), 32, 21, 124),
            (True, (124,), 95, 42, 124),
            (False, (107, 107, 107), 49, 21, 321),
            (True, (107, 107, 107), 180, 42, 321),
        ):
            with self.subTest(sharp=sharp, refs=refs):
                q = self.f.get_quote(compiled(sharp=sharp, video_frames=refs))
                self.assertEqual(q["credits"], amount)
                self.assertEqual(q["base_credits"], base)
                self.assertEqual(q["reference_video_credits"], amount - base)
                self.assertEqual(q["reference_video_frames"], ref_frames)
                self.assertEqual(q["reference_video_seconds"], ref_frames / 24)
                self.assertEqual(
                    q["breakdown"][0]["reference_video_credits"], amount - base
                )
        # A duplicated integer-ceiling of the base would overcharge this case.
        self.assertEqual(self.f.get_quote(compiled())["credits"], 32)

    def test_trimmed_excerpts_use_native_frames_and_denoise_stays_base(self):
        self.adopt()
        excerpts = [
            {"time_seconds": 1.3, "duration_seconds": 2.5},
            {"time_seconds": 3.7, "duration_seconds": 2.5},
        ]
        value = compiled(sharp=True, excerpts=excerpts)
        self.assertEqual(used_frames(value), 112)
        self.assertEqual(self.f.get_quote(value)["credits"], 90)
        for mode, expected in (("in_context", 95), ("denoise", 42)):
            q = self.f.get_quote(compiled(sharp=True, conditioning=mode))
            self.assertEqual(q["credits"], expected)
            self.assertEqual(q["denoise_control_unpriced"], mode == "denoise")

    def test_portrait_and_requested_duration_labels_cannot_discount(self):
        self.adopt()
        value = compiled(sharp=True, portrait=True)
        q = self.f.get_quote(value)
        self.assertEqual(q["credits"], 95)
        value["assets"]["v0"]["metadata"]["source_duration"] = 2
        self.assertEqual(self.f.get_quote(value)["credits"], 95)
        for field, changed in (
            ("quality_tier", "fast"),
            ("resolution", "480P"),
            ("duration", 4),
        ):
            spoofed = copy.deepcopy(value)
            spoofed["request"][field] = changed
            with self.assertRaisesRegex(PricingError, "credit_quote_invalid"):
                self.f.get_quote(spoofed)
        invalid = copy.deepcopy(value)
        invalid["assets"]["v0"]["metadata"]["frame_count"] = 500
        with self.assertRaisesRegex(PricingError, "credit_quote_invalid"):
            self.f.get_quote(invalid)
        with self.assertRaisesRegex(ValueError, "total_video_duration"):
            compiled(video_frames=(124, 124, 124))

    def test_in_context_video_guide_uses_the_native_used_frames(self):
        self.adopt()
        value = compiled(sharp=True, conditioning="in_context")
        value["request"]["inputs"]["videos"] = []
        value["request"]["guides"] = [{"media_id": "v0", "time_seconds": 0}]
        self.assertEqual(used_frames(value), 124)
        self.assertEqual(self.f.get_quote(value)["credits"], 95)

    def test_images_audio_and_renders_do_not_acquire_surcharge(self):
        self.adopt()
        value = compiled(video_frames=())
        value["request"]["inputs"]["audios"] = ["a"]
        value["assets"]["a"] = {"metadata": meta("audio")}
        self.assertEqual(self.f.get_quote(value)["reference_video_credits"], 0)
        rendered = self.f.get_quote(
            self.f.render_compiled(), execution={"backend": "cpu-render"}
        )
        self.assertEqual(rendered["credits"], 0)
        self.assertNotIn("reference_video_frames", rendered)

    def test_unmeasured_long_sharp_video_requires_qualification(self):
        value = compiled(sharp=True, duration=15)
        original = self.f.get_quote(value)
        self.adopt()
        with self.assertRaisesRegex(
            PricingError, "reference_video_duration_unqualified"
        ):
            self.f.get_quote(value)
        self.assertEqual(
            public_quote({"credit_quote": original})["credits"], original["credits"]
        )
        self.assertGreater(self.f.get_quote(compiled(duration=15))["credits"], 0)

    def test_invalid_policy_and_unsupported_video_compiler_fail_closed(self):
        config = {
            **copy.deepcopy(DEFAULT_CONFIGURATION),
            "resolution_multipliers": {"480P": 1, "768P": 2},
        }
        for invalid in (
            {"480P": True, "768P": 2500},
            {"480P": 1000},
            {"480P": -1, "768P": 2500},
            {"480P": 1000, "768P": 0.25},
            {"480P": 2500, "768P": 1000},
            {"480P": 1000, "768P": 100001},
            None,
        ):
            with self.subTest(invalid=invalid), self.assertRaises(PricingError):
                configuration({**config, FIELD: invalid})
        with self.assertRaises(PricingError):
            configuration({**DEFAULT_CONFIGURATION, FIELD: RECOMMENDED})
        self.adopt()
        value = compiled()
        value.pop("deployment_profile_id")
        with self.assertRaisesRegex(PricingError, "credit_quote_invalid"):
            self.f.get_quote(value)


class ReferenceClaimTests(LedgerCase):
    def setUp(self):
        super().setUp()
        self.queue = TaskQueue(self.repo)

    def new(
        self, *, scope=None, pool="test-pool", sharp=True, conditioning="reference"
    ):
        scope = scope or self.scope
        plan = self.repo.create_plan(
            scope,
            compiled(sharp=sharp, conditioning=conditioning),
            {"pool": pool, "expected_runtime_s": 120, "model_id": "test-model-only"},
            expires_at=self.now + 10000,
            estimated_cost_microusd=0,
        )
        return self.repo.create_job(
            scope, plan["id"], plan["id"], budget_account_ids=()
        )

    def test_single_owner_cross_pool_concurrent_claims_take_only_one(self):
        pools = ["pool-" + str(i) for i in range(8)]
        for pool in pools:
            self.new(pool=pool)
        result = self.parallel(lambda i: self.queue.claim("worker-" + str(i), pools[i]))
        self.assertEqual(sum(c is not None for c in result), 1)
        with self.repo.engine.connect() as c:
            self.assertEqual(len(c.execute(select(attempts.c.id)).all()), 1)

    def test_blocked_heavy_keeps_other_owners_and_light_work_eligible(self):
        first = self.new()
        claim = self.queue.claim("original", "test-pool")
        self.assertEqual(claim.job["id"], first["id"])
        blocked = self.new()
        light = self.new(sharp=False)
        denoise = self.new(conditioning="denoise")
        other = self.new(scope=self.other)
        self.assertIsNone(
            self.queue.claim(
                "blocked-delivery", "test-pool", selected_job_id=blocked["id"]
            )
        )
        served = [self.queue.claim("worker-" + str(i), "test-pool") for i in range(3)]
        self.assertEqual(
            {x.job["id"] for x in served}, {light["id"], denoise["id"], other["id"]}
        )
        self.assertIsNone(self.queue.claim("extra", "test-pool"))
        self.assertEqual(self.repo.get_job(self.scope, blocked["id"])["attempt_no"], 0)

    def test_unknown_attempt_blocks_new_heavy_until_original_terminal(self):
        self.new()
        claim = self.queue.claim("original", "test-pool")
        self.queue.begin_submission(claim.lease)
        next_job = self.new()
        self.now += 91
        self.queue.recover_expired()
        self.assertIsNone(self.queue.claim("new", "test-pool"))
        self.assertEqual(
            self.repo.get_job(self.scope, claim.job["id"])["status"],
            "submission_unknown",
        )
        self.repo.request_cancel(self.scope, claim.job["id"])
        recovery = self.queue.claim("observer", "test-pool", purpose="reconcile")
        self.assertEqual(recovery.lease.attempt_id, claim.lease.attempt_id)
        self.queue.confirm_cancel(
            recovery.lease, upstream_stopped=True, actual_cost_microusd=0
        )
        self.assertEqual(
            self.queue.claim("next", "test-pool").job["id"], next_job["id"]
        )

    def test_tenant_identity_and_failed_claim_do_not_reserve_new_slot(self):
        self.new()
        with patch.object(self.repo, "_emit", side_effect=RuntimeError("rollback")):
            with self.assertRaises(RuntimeError):
                self.queue.claim("fail", "test-pool")
        first = self.queue.claim("retry", "test-pool")
        self.assertIsNotNone(first)
        foreign = self.new(scope=Scope("other-tenant", self.scope.owner_id, "p"))
        self.assertEqual(
            self.queue.claim("foreign", "test-pool").job["id"], foreign["id"]
        )


if __name__ == "__main__":
    unittest.main()
