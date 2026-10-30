"""RunPod enhance backend (#795) against a fake RunPod API and store; no network."""

import io
import json
import pickle
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import httpx

from studio_platform import enhance_backend
from studio_platform.control import WorkerSpec
from studio_platform.enhance_backend import (
    Config,
    RunPodEnhanceBackend,
    load_config,
    temp_key,
)
from studio_platform.enhance_plans import compile_enhance
from studio_platform.execution_policy import ExecutionPolicies
from studio_platform.inference.outputs import _shape
from studio_platform.inference.protocol import (
    BackendError,
    SubmissionRejected,
    SubmissionUncertain,
)
from studio_platform.storage import SecretURL
from tests.test_enhance_plans import PROJECT, body, source


class Store:
    def __init__(self, size=20 * 1024**2, output=b"mp4"):
        self.size, self.output, self.uploads = size, output, []

    def stat(self, key):
        return SimpleNamespace(size_bytes=self.size, sha256="a" * 64)

    def presign_download(self, key, *, expires_seconds):
        return SecretURL("https://r2.test/get/" + key, "GET", 0, {})

    def presign_upload(self, key, *, content_type, expires_seconds):
        self.uploads.append((key, content_type))
        return SecretURL("https://r2.test/put/" + key, "PUT", 0, {})

    def open(self, key):
        return io.BytesIO(self.output)


def job(tier="2k"):
    compiled, _ = compile_enhance(body(tier), PROJECT, lambda e, k: source())
    return {"request": compiled, "execution_plan": {}}


class EnhanceBackendTests(unittest.TestCase):
    def backend(self, responses, store=None):
        self.requests = []

        def handle(request):
            self.requests.append(request)
            status, payload = responses.pop(0)
            if isinstance(payload, Exception):
                raise payload
            return httpx.Response(status, json=payload)

        backend = RunPodEnhanceBackend(
            store or Store(), "/unused", transport=httpx.MockTransport(handle)
        )
        backend._config = Config("key-1", "endpoint1", 970)
        return backend

    def test_config_file_needs_exactly_the_three_fields(self):
        good = {"api_key": "k", "endpoint_id": "abc123", "rate_microusd_per_s": 970}
        for value, valid in (
            (good, True),
            ({**good, "extra": 1}, False),
            ({**good, "endpoint_id": "../x"}, False),
            ({**good, "rate_microusd_per_s": 0}, False),
        ):
            # Ownership and mode checks are protected_config's own tests.
            with (
                self.subTest(value=value),
                mock.patch.object(
                    enhance_backend, "read_protected_json", return_value=value
                ),
            ):
                if valid:
                    self.assertEqual(load_config("/x").endpoint_id, "abc123")
                else:
                    self.assertRaises(ValueError, load_config, "/x")
        self.assertNotIn("secret-key", repr(Config("secret-key", "e", 1)))

    def test_one_run_with_presigned_urls_and_the_tier_settings(self):
        store = Store()
        backend = self.backend([(200, {"id": "job-1", "status": "IN_QUEUE"})], store)
        prepared = backend.prepare(job(), "attempt-1", None, lambda: None)
        self.assertEqual(backend.submit(prepared, "attempt-1"), "job-1")
        request = self.requests[0]
        self.assertEqual(str(request.url), "https://api.runpod.ai/v2/endpoint1/run")
        self.assertEqual(request.headers["authorization"], "Bearer key-1")
        sent = json.loads(request.content)
        self.assertEqual(
            sent["input"],
            {
                "video_url": "https://r2.test/get/k",
                "res": "2k",
                "steps": 3,
                "sigma": 0.8,
                "audio": "draft",
                "unet": "int8",
                "output_put_url": "https://r2.test/put/enhance-tmp/attempt-1.mp4",
            },
        )
        self.assertEqual(sent["policy"]["executionTimeout"], 900_000)
        self.assertEqual(store.uploads, [("enhance-tmp/attempt-1.mp4", "video/mp4")])
        self.assertRaises(TypeError, pickle.dumps, prepared)

    def test_changed_source_is_refused_before_any_run(self):
        backend = self.backend([], Store(size=1))
        with self.assertRaises(BackendError):
            backend.prepare(job(), "attempt-1", None, lambda: None)
        self.assertEqual(self.requests, [])

    def test_run_answers(self):
        cases = [
            ((401, {}), SubmissionRejected),
            ((500, {}), SubmissionUncertain),
            ((200, {"status": "IN_QUEUE"}), SubmissionUncertain),
            ((0, httpx.ReadTimeout("lost")), SubmissionUncertain),
        ]
        for response, error in cases:
            backend = self.backend([response])
            prepared = backend.prepare(job(), "attempt-1", None, lambda: None)
            with self.subTest(response=response), self.assertRaises(error):
                backend.submit(prepared, "attempt-1")
            self.assertEqual(len(self.requests), 1)

    def test_status_maps_to_outcomes_with_billed_seconds(self):
        timing = {"delayTime": 40_000, "executionTime": 61_500}
        cases = [
            ((200, {"id": "job-1", "status": "IN_PROGRESS"}), "running", None),
            (
                (
                    200,
                    {
                        "id": "job-1",
                        "status": "COMPLETED",
                        "output": {"uploaded": True},
                        **timing,
                    },
                ),
                "succeeded",
                98_455,
            ),
            (
                (200, {"id": "job-1", "status": "COMPLETED", "output": {}, **timing}),
                "failed",
                98_455,
            ),
            ((200, {"id": "job-1", "status": "TIMED_OUT", **timing}), "failed", 98_455),
            ((200, {"id": "job-1", "status": "CANCELLED"}), "cancelled", 0),
            ((200, {"id": "job-2", "status": "COMPLETED"}), "unknown", None),
            ((503, {}), "unknown", None),
        ]
        for response, state, cost in cases:
            outcome = self.backend([response]).poll("attempt-1", "job-1")
            with self.subTest(response=response):
                self.assertEqual(
                    (outcome.state, outcome.task_id, outcome.actual_cost_microusd),
                    (state, "job-1", cost),
                )
        self.assertEqual(self.backend([]).reconcile("attempt-1").state, "unknown")

    def test_fetch_returns_the_one_mp4_for_video_and_audio(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = self.backend([]).fetch(
                job(), "attempt-1", "job-1", Path(directory), lambda: None
            )
            self.assertEqual(paths["video"], paths["audio"])
            self.assertEqual(paths["video"].read_bytes(), b"mp4")
            self.assertEqual(
                sorted(p.name for p in Path(directory).iterdir()), ["enhance-raw.mp4"]
            )
            with self.assertRaises(BackendError):
                self.backend([], Store(output=b"")).fetch(
                    job(), "attempt-2", "job-1", Path(directory), lambda: None
                )
        self.assertRaises(BackendError, temp_key, "../x")

    def test_worker_spec_is_serverless_and_bound_to_the_recipe(self):
        args = ["w", "enhance", "runpod", "w", (), ("h3-enhance-v1",)]
        tail = ["open-h3-regenerate-2k", "runpod-enhance-v1", "runpod-enhance"]
        WorkerSpec(*args, *tail)
        for bad in (
            ["w", "enhance", "runpod", "w", ("gpu0",), ("h3-enhance-v1",)],
            ["w", "enhance", "local-cpu", "w", (), ("h3-enhance-v1",)],
            ["w", "enhance", "runpod", "w", (), ("h3-default",)],
        ):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                WorkerSpec(*bad, *tail)

    def test_output_shape_and_submission_recheck(self):
        enhance = job("1080p")
        self.assertEqual(_shape(enhance), (1920, 1080, 124 / 24, True))
        enhance["execution_plan"] = {
            "enabled": True,
            "backend": "runpod-enhance",
            "pool": "enhance",
            "configuration_id": "runpod-enhance-v1",
        }
        policies = ExecutionPolicies.__new__(ExecutionPolicies)
        for enabled in (True, False):
            policies.settings = SimpleNamespace(enhance_enabled=enabled)
            self.assertIs(policies.activation_allowed(enhance), enabled)


if __name__ == "__main__":
    unittest.main()
