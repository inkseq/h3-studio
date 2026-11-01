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
from studio_platform.capabilities import compile_request
from studio_platform.providers import everyframe as everyframe_backend
from studio_platform.providers import hosted_models as plan
from studio_platform.providers.everyframe import Config, EveryFrameBackend, load_config
from studio_platform.inference.protocol import (
    BackendError,
    SubmissionRejected,
    SubmissionUncertain,
)
from test_platform_api import generation_request

FAST = "h3-max-fast"


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
        body, refs = plan.vendor_request(compiled(seed="659"), FAST)
        self.assertEqual(
            body,
            {
                "modelId": "h3-max-fast",
                "prompt": body["prompt"],
                "duration": 5,
                "resolution": "480p",
                "aspect": "16:9",
                "referenceMode": "text",
            },
        )  # No seed: hosted models do not honour one (#929).
        self.assertTrue(body["prompt"].endswith(plan.NO_CUT))
        self.assertEqual(refs, [])
        inputs = {
            "first_frame": "a",
            "last_frame": None,
            "images": [],
            "videos": [],
            "audios": [],
        }
        body, refs = plan.vendor_request(compiled(inputs=inputs), FAST)
        self.assertEqual((body["referenceMode"], refs), ("first-frame", ["a"]))
        body, refs = plan.vendor_request(
            compiled(inputs={**inputs, "last_frame": "b"}), FAST
        )
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
        body, refs = plan.vendor_request(value, FAST)
        self.assertEqual(
            (body["referenceMode"], refs), ("omni", ["i1", "i2", "v1", "s1"])
        )
        # Turbo refuses references (400 bad_attachment), so it never gets them.
        self.assertIsNone(plan.vendor_request(value, "h3-max-turbo"))
        self.assertEqual(plan.chain(value), ["h3-max-fast"])

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
                self.assertIsNone(plan.vendor_request(compiled(**changes), FAST))
        value = compiled()
        value["recipe_id"] = "chapter-roughcut-v1"
        self.assertIsNone(plan.vendor_request(value, FAST))
        self.assertIsNone(plan.vendor_request(compiled(), "seedance-2-5"))

    def test_chains_follow_price_within_what_the_take_needs(self):
        self.assertEqual(
            plan.chain(compiled()), ["h3-max-turbo", "pika-h3-flex", "h3-max-fast"]
        )
        self.assertEqual(
            plan.chain(compiled(), "economy"), ["vivix-w1", "h3-max-turbo"]
        )
        self.assertEqual(plan.chain(compiled(resolution="2K")), ["pika-h3-flex"])
        self.assertAlmostEqual(plan.price("h3-max-turbo", compiled(duration=10)), 0.076)
        # Pika does not cut on its own, so its prompt is left alone.
        body, _ = plan.vendor_request(compiled(), "pika-h3-flex")
        self.assertNotIn(plan.NO_CUT, body["prompt"])
        # Vivix always renders 720p; Pika takes 2K under the vendor's own name.
        body, _ = plan.vendor_request(compiled(), "vivix-w1")
        self.assertEqual(body["resolution"], "720p")
        body, _ = plan.vendor_request(compiled(resolution="2K"), "pika-h3-flex")
        self.assertEqual(body["resolution"], "2K")
        self.assertIsNone(
            plan.vendor_request(compiled(aspect_ratio="21:9"), "vivix-w1")
        )
        self.assertIsNone(
            plan.vendor_request(compiled(prompt="x" * 8000), "pika-h3-flex")
        )
        # Refusing models are not in the catalog at all (owner, 2026-11-01).
        self.assertFalse({"seedance-2-5", "kling-4-0"} & set(plan.MODELS))


class Store:
    def __init__(self, size=3):
        self.size = size

    def stat(self, key):
        return SimpleNamespace(size_bytes=self.size, sha256=None)

    def open(self, key):
        return io.BytesIO(b"png"[: self.size])


def job(value=None, model=FAST):
    return {
        "request": value or compiled(),
        "execution_plan": {"configuration_id": plan.MODELS[model].configuration_id},
    }


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
            (sent["referenceMode"], sent["attachmentIds"], "seed" in sent),
            ("first-frame", ["att_1"], False),
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


if __name__ == "__main__":
    unittest.main()
