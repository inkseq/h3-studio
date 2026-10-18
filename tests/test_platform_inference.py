"""Extraction compatibility and fake private HTTP only; no inference runtime."""
from dataclasses import FrozenInstanceError
import json
from pathlib import Path
import subprocess
import sys
import textwrap
import unittest
from unittest.mock import patch

import httpx

from studio_platform.inference.comfy import ComfyBackend
from studio_platform.inference import outputs, protocol


class InferenceExtractionTests(unittest.TestCase):
    def test_legacy_imports_keep_class_and_function_identity(self):
        from studio_platform import worker
        self.assertIs(worker.ComfyBackend, ComfyBackend)
        for name in ("BackendError", "NotReady", "SubmissionUncertain", "SubmissionRejected",
                     "RenderCacheCapacityExceeded", "Outcome", "TAG", "TASK"):
            self.assertIs(getattr(worker, name), getattr(protocol, name), name)
        self.assertIs(worker._shape, outputs._shape)
        self.assertIs(worker._request, outputs._request)
        outcome = protocol.Outcome("unknown", "original-task")
        self.assertEqual(outcome, worker.Outcome("unknown", "original-task", None))
        with self.assertRaises(FrozenInstanceError):
            outcome.state = "succeeded"
        with self.assertRaises(worker.BackendError):
            raise protocol.SubmissionUncertain("synthetic_unknown")

    def test_adapter_import_is_independent_and_construction_has_no_effects(self):
        # A fresh process exposes accidental imports hidden by test-suite order.
        script = textwrap.dedent("""
            import sys
            from unittest.mock import patch
            import httpx
            with patch('httpx.Client', side_effect=AssertionError('no HTTP')), \\
                 patch('subprocess.Popen', side_effect=AssertionError('no process')), \\
                 patch('pathlib.Path.mkdir', side_effect=AssertionError('no directory')):
                from studio_platform.inference.comfy import ComfyBackend
                from studio_platform.inference import protocol, outputs
                assert ComfyBackend().enabled is False
                explicit = ComfyBackend(endpoint='http://127.0.0.1:8188', enabled=True,
                    allowed_origins=('http://127.0.0.1:8188',))
                assert explicit._client is None
                assert not any(name in sys.modules for name in (
                    'studio_platform.worker', 'studio_platform.queue',
                    'studio_platform.settings', 'studio_platform.repository',
                    'comfy_workflow', 'torch', 'api_registry', 'shared.api'))
        """)
        result = subprocess.run([sys.executable, "-B", "-c", script],
            cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_idle_probe_requires_both_current_queues_to_be_empty(self):
        for response, expected in (({"queue_running": [], "queue_pending": []}, True),
                ({"queue_running": [[0, "foreign-task"]], "queue_pending": []}, False),
                ({"queue_running": [], "queue_pending": [[1, "foreign-task"]]}, False),
                ({"queue_running": [], "queue_pending": None}, False),
                ({"queue_running": [], "queue_pending": {}}, False),
                ({"queue_running": []}, False), ([], False), (None, False)):
            with self.subTest(response=response):
                seen = []
                def handler(request):
                    seen.append((request.method, request.url.path))
                    return httpx.Response(200, content=json.dumps(response).encode())
                backend = ComfyBackend(endpoint="http://127.0.0.1:8188", enabled=True,
                    allowed_origins=("http://127.0.0.1:8188",), transport=httpx.MockTransport(handler))
                try:
                    self.assertIs(backend.is_idle(), expected)
                    self.assertEqual(seen, [("GET", "/queue")])
                finally:
                    backend.close()

    def test_failed_or_disabled_idle_probe_never_reports_ready(self):
        with patch("httpx.Client", side_effect=AssertionError("disabled performs no HTTP")):
            with self.assertRaisesRegex(protocol.NotReady, "backend_disabled"):
                ComfyBackend().is_idle()
        for status, content in ((200, b"invalid-json"), (503, b"unavailable"), (302, b"")):
            with self.subTest(status=status):
                backend = ComfyBackend(endpoint="http://127.0.0.1:8188", enabled=True,
                    allowed_origins=("http://127.0.0.1:8188",),
                    transport=httpx.MockTransport(lambda _: httpx.Response(status, content=content)))
                try:
                    with self.assertRaises(protocol.BackendError):
                        backend.is_idle()
                finally:
                    backend.close()


if __name__ == "__main__":
    unittest.main()
