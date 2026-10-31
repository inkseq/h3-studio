"""Hosted cold-start backstop (#918) against a fake vendor API and store; no network."""

import copy
import io
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import httpx
from sqlalchemy import update

from studio_platform import everyframe_backend, everyframe_plans as plan
from studio_platform.capabilities import compile_request
from studio_platform.control import WorkerSpec
from studio_platform.everyframe_backend import Config, EveryFrameBackend, load_config
from studio_platform.inference.protocol import (
    BackendError,
    SubmissionRejected,
    SubmissionUncertain,
)
from studio_platform.repository import registered_workers
from test_platform_api import generation_request
import test_platform_execution_policy as base


def compiled(**changes):
    value, _ = compile_request(generation_request(), lambda _: None)
    value = copy.deepcopy(value)
    value["request"].update(changes)
    return value


def asset(content_type="image/png", size=3):
    return {
        "metadata": {"kind": "image"},
        "model": {
            "key": "owners/a/" + content_type,
            "content_type": content_type,
            "size_bytes": size,
        },
    }


class VendorRequestTests(unittest.TestCase):
    def test_text_first_and_first_last_map_to_vendor_modes(self):
        body, refs = plan.vendor_request(compiled(seed="659"))
        self.assertEqual(
            body,
            {
                "modelId": "h3-max-fast",
                "prompt": body["prompt"],
                "duration": 5,
                "resolution": "480p",
                "aspect": "16:9",
                "referenceMode": "text",
                "seed": 659,
            },
        )
        self.assertEqual(refs, [])
        inputs = {
            "first_frame": "a",
            "last_frame": None,
            "images": [],
            "videos": [],
            "audios": [],
        }
        body, refs = plan.vendor_request(compiled(inputs=inputs))
        self.assertEqual((body["referenceMode"], refs), ("first-frame", ["a"]))
        body, refs = plan.vendor_request(compiled(inputs={**inputs, "last_frame": "b"}))
        self.assertEqual((body["referenceMode"], refs), ("first-last", ["a", "b"]))

    def test_reference_mode_sends_every_reference_kind_in_order(self):
        value = compiled(
            mode="ref",
            inputs={
                "first_frame": None,
                "last_frame": None,
                "images": ["i1", "i2"],
                "videos": ["v1"],
                "audios": ["s1"],
            },
        )
        value["recipe_id"] = "h3-base-ref2va-v1"
        body, refs = plan.vendor_request(value)
        self.assertEqual(
            (body["referenceMode"], refs), ("omni", ["i1", "i2", "v1", "s1"])
        )

    def test_anything_not_exactly_representable_stays_on_our_machines(self):
        inputs = {
            "first_frame": None,
            "last_frame": "b",
            "images": [],
            "videos": [],
            "audios": [],
        }
        for changes in (
            {"guides": [{"frame": 1}]},
            {"video_excerpts": [{"start": 0}]},
            {"audio_excerpts": [{"start": 0}]},
            {"video_audio": {"v1": True}},
            {"resolution": "2K"},
            {"aspect_ratio": "3:2"},
            {"duration": 20},
            {"inputs": inputs},
            {"inputs": {**inputs, "last_frame": None, "images": ["i"] * 10}},
            {"inputs": {**inputs, "last_frame": None, "images": ["i"]}},
        ):
            with self.subTest(changes=changes):
                self.assertIsNone(plan.vendor_request(compiled(**changes)))
        value = compiled()
        value["recipe_id"] = "chapter-roughcut-v1"
        self.assertIsNone(plan.vendor_request(value))

    def test_worker_identity_is_fixed(self):
        good = (
            "w",
            plan.POOL,
            plan.PROVIDER,
            "w",
            (),
            plan.RECIPES,
            plan.MODEL,
            plan.CONFIGURATION,
            plan.BACKEND,
        )
        WorkerSpec(*good)
        for index, value in ((2, "runpod"), (4, ("gpu",)), (6, "other"), (7, "x-v2")):
            with self.subTest(index=index):
                bad = list(good)
                bad[index] = value
                self.assertRaises(ValueError, WorkerSpec, *bad)


class Store:
    def __init__(self, size=3):
        self.size = size

    def stat(self, key):
        return SimpleNamespace(size_bytes=self.size, sha256=None)

    def open(self, key):
        return io.BytesIO(b"png"[: self.size])


def job(value=None):
    return {"request": value or compiled(), "execution_plan": {}}


class BackendTests(unittest.TestCase):
    def backend(self, responses, store=None):
        self.requests = []

        def handle(request):
            self.requests.append(request)
            status, payload = responses.pop(0)
            if isinstance(payload, Exception):
                raise payload
            if isinstance(payload, bytes):
                return httpx.Response(status, content=payload)
            return httpx.Response(status, json=payload)

        backend = EveryFrameBackend(
            store or Store(), "/unused", transport=httpx.MockTransport(handle)
        )
        backend._config = Config("key-1")
        return backend

    def test_config_holds_only_the_key_and_never_shows_it(self):
        for value, valid in (
            ({"api_key": "k"}, True),
            ({"api_key": "k", "x": 1}, False),
            ({"api_key": "a b"}, False),
        ):
            with (
                self.subTest(value=value),
                mock.patch.object(
                    everyframe_backend, "read_protected_json", return_value=value
                ),
            ):
                if valid:
                    self.assertEqual(load_config("/x").api_key, "k")
                else:
                    self.assertRaises(ValueError, load_config, "/x")
        self.assertNotIn("secret-key", repr(Config("secret-key")))

    def test_attachments_then_one_render_with_the_mapped_body(self):
        value = compiled(
            seed="659",
            inputs={
                "first_frame": "a",
                "last_frame": None,
                "images": [],
                "videos": [],
                "audios": [],
            },
        )
        value["assets"] = {"a": asset()}
        backend = self.backend(
            [
                (201, {"attachment": {"id": "att_1"}}),
                (202, {"render": {"id": "rnd_1", "status": "queued"}, "balanceUsd": 4}),
            ]
        )
        prepared = backend.prepare(job(value), "attempt-1", None, lambda: None)
        self.assertEqual(backend.submit(prepared, "attempt-1"), "rnd_1")
        upload, render = self.requests
        self.assertEqual(
            str(upload.url), "https://api.everyframe.studio/v1/render/attachment"
        )
        self.assertEqual(
            (upload.headers["content-type"], upload.content), ("image/png", b"png")
        )
        self.assertEqual(render.headers["authorization"], "Bearer key-1")
        sent = json.loads(render.content)
        self.assertEqual(
            (sent["referenceMode"], sent["attachmentIds"], sent["seed"]),
            ("first-frame", ["att_1"], 659),
        )

    def test_changed_input_is_refused_before_any_render(self):
        value = compiled(
            inputs={
                "first_frame": "a",
                "last_frame": None,
                "images": [],
                "videos": [],
                "audios": [],
            }
        )
        value["assets"] = {"a": asset(size=4)}
        backend = self.backend([])
        with self.assertRaises(BackendError):
            backend.prepare(job(value), "attempt-1", None, lambda: None)
        self.assertEqual(self.requests, [])

    def test_refusal_costs_nothing_and_a_lost_answer_is_never_resubmitted(self):
        for status in (400, 402, 422, 429):
            backend = self.backend([(status, {"code": "x"})])
            prepared = backend.prepare(job(), "t", None, lambda: None)
            with self.subTest(status=status), self.assertRaises(SubmissionRejected):
                backend.submit(prepared, "t")
        for answer in ((500, {}), (202, {"render": {}}), (0, httpx.ConnectError("x"))):
            backend = self.backend([answer])
            prepared = backend.prepare(job(), "t", None, lambda: None)
            with self.subTest(answer=answer[0]), self.assertRaises(SubmissionUncertain):
                backend.submit(prepared, "t")

    def test_poll_maps_states_and_records_the_vendor_cost(self):
        url = "/render/rnd_1/video?t=x&full=1"
        cases = (
            ({"status": "running"}, "running", None),
            (
                {"status": "succeeded", "costUsd": 0.12, "videoFullUrl": url},
                "succeeded",
                120000,
            ),
            ({"status": "succeeded", "costUsd": 0.12}, "failed", 0),
            ({"status": "failed", "costUsd": 0.12}, "failed", 0),
        )
        for render, state, cost in cases:
            backend = self.backend([(200, {"render": {"id": "rnd_1", **render}})])
            outcome = backend.poll("t", "rnd_1")
            with self.subTest(render=render):
                self.assertEqual(
                    (outcome.state, outcome.actual_cost_microusd), (state, cost)
                )
        backend = self.backend([(404, {})])
        self.assertEqual(backend.poll("t", "rnd_1").state, "unknown")
        self.assertFalse(backend.cancel("t", "rnd_1"))

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg is required")
    def test_fetch_matches_the_planned_size_duration_and_audio(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "vendor.mp4"
            subprocess.run(
                [
                    "ffmpeg",
                    "-loglevel",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "testsrc=size=848x480:rate=24:duration=5",
                    "-f",
                    "lavfi",
                    "-i",
                    "sine=duration=5",
                    "-shortest",
                    "-pix_fmt",
                    "yuv420p",
                    source,
                ],
                check=True,
            )
            url = "/render/rnd_1/video?t=x&full=1"
            backend = self.backend(
                [
                    (
                        200,
                        {
                            "render": {
                                "id": "rnd_1",
                                "status": "succeeded",
                                "videoFullUrl": url,
                            }
                        },
                    ),
                    (200, source.read_bytes()),
                ]
            )
            target = Path(directory) / "out"
            target.mkdir()
            paths = backend.fetch(job(), "t", "rnd_1", target, lambda: None)
            info = json.loads(
                subprocess.run(
                    [
                        "ffprobe",
                        "-v",
                        "error",
                        "-show_entries",
                        "stream=codec_type,width,height:format=duration",
                        "-of",
                        "json",
                        paths["video"],
                    ],
                    capture_output=True,
                    check=True,
                ).stdout
            )
            video = next(s for s in info["streams"] if s["codec_type"] == "video")
            self.assertEqual((video["width"], video["height"]), (832, 480))
            self.assertGreaterEqual(float(info["format"]["duration"]), 5.16)
            self.assertTrue(paths["audio"].is_file())
            self.assertEqual(
                sorted(p.name for p in target.iterdir()),
                ["everyframe-audio.flac", "everyframe-video.mp4"],
            )


class RoutingTests(unittest.TestCase):
    """Reuses the synthetic policy and warm worker of the policy tests."""

    write = base.ExecutionPolicyTests.write
    evaluate = base.ExecutionPolicyTests.evaluate

    def setUp(self):
        base.ExecutionPolicyTests.setUp(self)
        from dataclasses import replace

        self.settings = replace(self.settings, everyframe_enabled=True)
        from studio_platform.execution_policy import ExecutionPolicies

        self.policies = ExecutionPolicies(self.settings, self.repo)
        self.control.register(
            WorkerSpec(
                "hosted-0",
                plan.POOL,
                plan.PROVIDER,
                "hosted-0",
                (),
                plan.RECIPES,
                plan.MODEL,
                plan.CONFIGURATION,
                plan.BACKEND,
            )
        )
        self.control.mark_ready("hosted-0", upstream_idle_confirmed=True)

    def cool(self):
        with self.repo.transaction() as conn:
            conn.execute(
                update(registered_workers)
                .where(registered_workers.c.id == "synthetic-worker")
                .values(expires_at=self.repo.clock() - 1)
            )

    def test_warm_machine_keeps_the_take(self):
        self.assertEqual(self.evaluate().execution["pool"], "synthetic-pool")

    def test_cold_take_goes_to_the_hosted_slot_with_the_same_tier(self):
        self.cool()
        execution = self.evaluate().execution
        self.assertEqual(
            (execution["pool"], execution["backend"], execution["enabled"]),
            (plan.POOL, plan.BACKEND, True),
        )
        self.assertEqual(execution["routed_from"]["pool"], "synthetic-pool")
        self.assertEqual(execution["budget_account_ids"], [])
        admission = self.evaluate()
        created = self.repo.create_plan(
            self.scope,
            self.compiled,
            admission.execution,
            expires_at=admission.expires_at,
            estimated_cost_microusd=0,
        )
        created_job = self.repo.create_job(
            self.scope, created["id"], "one", budget_account_ids=()
        )
        self.assertTrue(self.policies.activation_allowed(created_job))
        self.assertIsNotNone(self.control.claim("hosted-0", plan.POOL))

    def test_plan_keeps_its_route_when_a_machine_warms_up(self):
        self.cool()
        hosted = self.evaluate()
        self.control.mark_ready("synthetic-worker", upstream_idle_confirmed=True)
        record = {"request": self.compiled, "execution_plan": hosted.execution}
        self.assertEqual(self.policies.ensure_current(record, self.scope), ())

    def test_backstop_off_unrepresentable_or_window_used_stays_cold(self):
        self.cool()
        from dataclasses import replace
        from studio_platform.execution_policy import ExecutionPolicies

        off = ExecutionPolicies(
            replace(self.settings, everyframe_enabled=False), self.repo
        )
        self.assertEqual(
            off.evaluate(self.compiled, self.scope, self.fingerprint).execution["pool"],
            "synthetic-pool",
        )
        value = copy.deepcopy(self.compiled)
        value["request"]["guides"] = [{"frame": 1}]
        self.assertIsNone(self.policies._evaluate_everyframe(value, self.scope, "f"))
        with mock.patch.object(plan, "MAX_PER_WINDOW", 0):
            self.assertEqual(self.evaluate().execution["pool"], "synthetic-pool")


if __name__ == "__main__":
    unittest.main()
