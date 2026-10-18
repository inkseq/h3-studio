"""Private synthetic HTTP only; no listener, provider, model or inference."""
from dataclasses import asdict
import hashlib
import io
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient
import httpx

from studio_platform.inference.protocol import SubmissionUncertain, SubmissionRejected
from studio_platform.inference.wangp_contract import (
    HostReadiness, InputDescriptor, OperationReceipt, PreparedRequest, canonical_json)
from studio_platform.inference.wangp_http import HTTPWanGPTransport
from studio_platform.runtime_hosts.wangp_http import StagedInputs, create_app

TOKEN = "synthetic-test-only-" + "x" * 32


def prepared():
    return PreparedRequest("job-test", "attempt-test", "1" * 64, "2" * 64,
        canonical_json({"prompt": "synthetic-private-prompt"}),
        canonical_json({"width": 64, "height": 64}), True)


class FakeHost:
    def __init__(self):
        self.calls = 0
        self.receipt = None
        self.incarnation = "a" * 32

    def readiness(self):
        return HostReadiness("2" * 64, "slot-test", self.incarnation, True)

    def submit(self, value):
        self.calls += 1
        self.receipt = OperationReceipt(value.operation_id, value.job_id, value.attempt_tag,
            value.request_hash, value.manifest_digest, value.identity_digest,
            "slot-test", self.incarnation, "running", True)
        return self.receipt

    def inspect(self, identity):
        return self.receipt if self.receipt and self.receipt.operation_id == identity else None

    def cancel(self, identity):
        return identity == self.receipt.operation_id


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.inputs = StagedInputs(Path(self.temp.name), max_bytes=1024)
        self.host = FakeHost()
        self.client = TestClient(create_app(self.host, self.inputs, token=TOKEN))
        self.addCleanup(self.client.close)

        def dispatch(request):
            response = self.client.request(request.method, request.url.raw_path.decode(),
                headers=dict(request.headers), content=request.read())
            return httpx.Response(response.status_code, content=response.content,
                                  headers=dict(response.headers))
        self.dispatch = dispatch
        self.transport = HTTPWanGPTransport("http://127.0.0.1:8199", TOKEN,
                                            transport=httpx.MockTransport(dispatch))
        self.addCleanup(self.transport.close)

    def test_authentication_is_required_before_host_access(self):
        response = self.client.post("/v1/operations", json=asdict(prepared()))
        self.assertEqual(response.status_code, 401)
        self.assertEqual(self.host.calls, 0)
        self.assertNotIn(TOKEN, response.text)

    def test_transport_submit_inspect_cancel_and_readiness(self):
        self.assertTrue(self.transport.readiness().idle)
        value = prepared()
        self.assertIsNone(self.transport.inspect(value.operation_id))
        receipt = self.transport.submit(value)
        self.assertEqual(self.transport.inspect(value.operation_id), receipt)
        self.assertTrue(self.transport.cancel(value.operation_id))
        self.assertEqual(self.host.calls, 1)

    def bound_transport(self, incarnation):
        transport = HTTPWanGPTransport("http://127.0.0.1:8199", TOKEN,
            transport=httpx.MockTransport(self.dispatch), expected_incarnation=incarnation)
        self.addCleanup(transport.close)
        return transport

    def test_bound_submit_checks_epoch_at_server_before_dispatch(self):
        transport = self.bound_transport(self.host.incarnation)
        self.assertEqual(transport.submit(prepared()).incarnation, self.host.incarnation)
        self.assertEqual(self.host.calls, 1)

    def test_replacement_after_readiness_cannot_dispatch_but_old_receipt_remains_readable(self):
        transport = self.bound_transport(self.host.incarnation)
        original = transport.submit(prepared())
        observed = transport.readiness()
        self.host.incarnation = "b" * 32  # Replacement at the same private endpoint.
        self.assertNotEqual(observed.incarnation, self.host.incarnation)
        # Even a rejected POST remains conservatively uncertain to the client:
        # it cannot disprove a previously accepted response lost on the network.
        with self.assertRaises(SubmissionUncertain):
            transport.submit(prepared())
        self.assertEqual(self.host.calls, 1)
        self.assertEqual(transport.inspect(original.operation_id), original)
        self.assertTrue(transport.cancel(original.operation_id))

    def test_wrong_or_malformed_epoch_is_rejected_without_returning_identity(self):
        for value in ("b" * 32, "invalid-epoch", "", "a" * 33):
            with self.subTest(length=len(value)):
                response = self.client.post("/v1/operations", json=asdict(prepared()), headers={
                    "Authorization": "Bearer " + TOKEN, "X-Wangp-Incarnation": value})
                self.assertEqual(response.status_code, 409)
                self.assertEqual(response.json(), {"error": "wangp_runtime_incarnation_mismatch"})
                self.assertEqual(self.host.calls, 0)
        for value in (False, "invalid-epoch", "", "a" * 33):
            with self.assertRaisesRegex(ValueError, "invalid_runtime_incarnation"):
                HTTPWanGPTransport("http://127.0.0.1:8199", TOKEN, expected_incarnation=value)

    def test_invalid_input_is_rejected_before_host_dispatch(self):
        value = asdict(prepared())
        value["attempt_tag"] = "../../invalid"
        response = self.client.post("/v1/operations", json=value,
                                    headers={"Authorization": "Bearer " + TOKEN})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.host.calls, 0)

    def test_host_error_is_unknown_and_does_not_expose_private_data(self):
        def broken(value):
            self.host.calls += 1
            raise RuntimeError("synthetic-private-prompt " + TOKEN)
        self.host.submit = broken
        with self.assertRaisesRegex(SubmissionUncertain, "wangp_private_submission_unknown"):
            self.transport.submit(prepared())
        self.assertEqual(self.host.calls, 1)

    def test_remote_endpoint_and_redirect_are_not_followed(self):
        for endpoint in ("http://example.org:123", "http://127.0.0.1:123/?token=x",
                         "http://user@127.0.0.1:123", "http://127.0.0.1:123/path"):
            with self.assertRaises(ValueError):
                HTTPWanGPTransport(endpoint, TOKEN)
        calls = []
        def redirect(request):
            calls.append(request.url)
            return httpx.Response(307, headers={"Location": "https://example.org"})
        transport = HTTPWanGPTransport("http://127.0.0.1:8199", TOKEN,
                                      transport=httpx.MockTransport(redirect))
        self.addCleanup(transport.close)
        with self.assertRaises(SubmissionUncertain):
            transport.submit(prepared())
        self.assertEqual(len(calls), 1)

    def test_stage_replays_same_content_and_resolves_physical_file(self):
        data = b"synthetic-input-bytes"
        descriptor = InputDescriptor("asset-one", "opaque-input", "image",
                                     hashlib.sha256(data).hexdigest(), len(data))
        self.assertEqual(self.transport.stage_input(descriptor, io.BytesIO(data)), descriptor)
        self.transport.stage_input(descriptor, io.BytesIO(data))
        actual = self.inputs.resolve(descriptor)
        self.assertEqual(actual.read_bytes(), data)
        actual.write_bytes(b"x" * len(data))
        with self.assertRaisesRegex(ValueError, "mismatch"):
            self.inputs.resolve(descriptor)

    def test_staging_never_admits_bad_hash_or_oversize(self):
        descriptor = InputDescriptor("asset-one", "input-one", "image", "0" * 64, 3)
        with self.assertRaisesRegex(Exception, "stage_failed"):
            self.transport.stage_input(descriptor, io.BytesIO(b"abc"))
        with self.assertRaises(ValueError):
            self.inputs.key(InputDescriptor("asset-one", "input-one", "image", "0" * 64, 2048))

    def test_image_materialization_preserves_normalized_bytes_and_rejects_tampering(self):
        from PIL import Image
        from studio_platform.runtime_hosts.wangp_launcher import resolve_inputs
        from dataclasses import replace
        data = io.BytesIO()
        Image.new("RGB", (256, 256), "navy").save(data, format="PNG")
        raw = data.getvalue()
        item = InputDescriptor("asset-one", "input-one", "image", hashlib.sha256(raw).hexdigest(), len(raw))
        self.transport.stage_input(item, io.BytesIO(raw))
        request = replace(prepared(), settings_json=canonical_json({"seed": 42, "image_start": item.handle, "image_end": None}),
                          inputs=(item,))
        actual = Path(resolve_inputs(request, self.inputs)["image_start"])
        self.assertEqual(actual.suffix, ".png")
        self.assertEqual(actual.read_bytes(), raw)
        self.assertEqual(self.inputs.image_path(item), actual)
        actual.write_bytes(b"x" * len(raw))
        with self.assertRaisesRegex(ValueError, "mismatch"):
            self.inputs.image_path(item)


if __name__ == "__main__":
    unittest.main()
