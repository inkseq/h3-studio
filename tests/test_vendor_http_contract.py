"""Freeze the two vendor HTTP contracts before sharing their transport code."""

import io
import json
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

import httpx

from studio_platform import enhance_backend as runpod
from studio_platform.providers import everyframe
from studio_platform.inference.protocol import (
    BackendError,
    SubmissionRejected,
    SubmissionUncertain,
)


class Stream(httpx.SyncByteStream):
    def __init__(self, chunks):
        self.chunks, self.closed = chunks, False
        self.read_chunks = 0

    def __iter__(self):
        for chunk in self.chunks:
            self.read_chunks += 1
            if isinstance(chunk, Exception):
                raise chunk
            yield chunk

    def close(self):
        self.closed = True


class Transport(httpx.MockTransport):
    def __init__(self, handler):
        super().__init__(handler)
        self.closed = False

    def close(self):
        self.closed = True


class VendorHTTPContractTests(unittest.TestCase):
    def backend(self, vendor, status=200, content=b"{}", *, stream=None, error=None):
        self.requests = []

        def handle(request):
            self.requests.append(request)
            if error:
                raise error
            return httpx.Response(
                status,
                content=content if stream is None else None,
                stream=stream,
                headers={"Location": "https://foreign.invalid/private"},
            )

        transport = Transport(handle)
        if vendor == "everyframe":
            backend = everyframe.EveryFrameBackend(None, "/unused", transport=transport)
            backend._config = everyframe.Config("sentinel-private-key")
        else:
            backend = runpod.RunPodEnhanceBackend(None, "/unused", transport=transport)
            backend._config = runpod.Config("sentinel-private-key", "endpoint1", 970)
        self.addCleanup(backend.close)
        return backend, transport

    def test_status_body_and_empty_distinctions(self):
        for vendor in ("everyframe", "runpod"):
            for status in (
                200,
                201,
                204,
                400,
                401,
                404,
                408,
                409,
                422,
                429,
                500,
                302,
                307,
                308,
            ):
                for body in (b'{"ok":true}', b"", b"[]", b"malformed", b"null"):
                    with self.subTest(vendor=vendor, status=status, body=body):
                        backend, _ = self.backend(vendor, status, body)
                        parse = vendor == "everyframe" or 200 <= status < 300
                        invalid = parse and (
                            body in (b"[]", b"malformed")
                            or body == b""
                            and vendor == "runpod"
                        )
                        if invalid:
                            with self.assertRaises(BackendError):
                                backend._call("GET", "/key")
                        else:
                            expected = json.loads(body) if parse and body else None
                            self.assertEqual(
                                backend._call("GET", "/key"), (status, expected)
                            )
                        self.assertEqual(len(self.requests), 1)

    def test_submit_rejection_and_uncertainty_never_retry_or_follow(self):
        for vendor in ("everyframe", "runpod"):
            for status in (400, 401, 403, 404, 408, 409, 422, 429, 500, 302, 307, 308):
                for body in (b"{}", b"malformed"):
                    with self.subTest(vendor=vendor, status=status, body=body):
                        backend, _ = self.backend(vendor, status, body)
                        rejected = (
                            400 <= status < 500 and body == b"{}"
                            if vendor == "everyframe"
                            else status in (400, 401, 403, 404)
                        )
                        prepared = (
                            everyframe if vendor == "everyframe" else runpod
                        ).Prepared("tag", b"{}")
                        with self.assertRaises(
                            SubmissionRejected if rejected else SubmissionUncertain
                        ):
                            backend.submit(prepared, "tag")
                        self.assertEqual(len(self.requests), 1)
                        self.assertEqual(backend.reconcile("tag").state, "unknown")
                        self.assertEqual(len(self.requests), 1)

    def test_response_limit_stream_failure_and_close(self):
        limit = 1024**2
        for vendor in ("everyframe", "runpod"):
            for chunks, valid in (
                ([b"{}" + b" " * (limit - 3)], True),
                ([b"{}" + b" " * (limit - 2)], True),
                ([b"{}" + b" " * (limit - 1)], False),
                ([b"{}", httpx.ReadError("sentinel-private-body")], False),
            ):
                with self.subTest(
                    vendor=vendor,
                    size=sum(len(c) for c in chunks if isinstance(c, bytes)),
                ):
                    stream = Stream(chunks)
                    backend, _ = self.backend(vendor, stream=stream)
                    if valid:
                        self.assertEqual(backend._call("GET", "/key"), (200, {}))
                    else:
                        with self.assertRaises(BackendError):
                            backend._call("GET", "/key")
                    self.assertTrue(stream.closed)
                    self.assertEqual(len(self.requests), 1)

    def test_explicit_headers_body_timeouts_and_lazy_close_ownership(self):
        for vendor in ("everyframe", "runpod"):
            with self.subTest(vendor=vendor):
                backend, transport = self.backend(vendor)
                self.assertEqual(self.requests, [])
                backend.close()
                self.assertEqual(transport.closed, vendor == "runpod")
                backend, transport = self.backend(vendor)
                backend._call("POST", "/run", {"input": "value"})
                request = self.requests[0]
                self.assertEqual(request.content, b'{"input": "value"}')
                self.assertEqual(
                    request.headers["authorization"], "Bearer sentinel-private-key"
                )
                self.assertEqual(request.headers["content-type"], "application/json")
                seconds = 60 if vendor == "everyframe" else 30
                self.assertEqual(
                    request.extensions["timeout"],
                    dict.fromkeys(("connect", "read", "write", "pool"), seconds),
                )
                backend._call("GET", "/status")
                request = self.requests[-1]
                if vendor == "runpod":
                    self.assertEqual(request.headers["accept"], "application/json")
                    self.assertEqual(
                        request.headers["content-type"], "application/json"
                    )
                    self.assertNotIn("accept-encoding", request.headers)
                    self.assertNotIn("user-agent", request.headers)
                else:
                    self.assertNotIn("content-type", request.headers)
                backend.close()
                self.assertTrue(transport.closed)

    def test_transport_error_and_disabled_backend_do_not_leak_or_submit_again(self):
        for vendor in ("everyframe", "runpod"):
            backend, _ = self.backend(
                vendor,
                error=httpx.ReadTimeout("sentinel-private-key https://private.invalid"),
            )
            prepared = (everyframe if vendor == "everyframe" else runpod).Prepared(
                "tag", b"{}"
            )
            out = io.StringIO()
            with redirect_stdout(out), self.assertRaises(SubmissionUncertain) as caught:
                backend.submit(prepared, "tag")
            self.assertNotIn(
                "sentinel", str(caught.exception) + out.getvalue() + repr(backend)
            )
            self.assertNotIn("private.invalid", str(caught.exception) + out.getvalue())
            self.assertEqual(len(self.requests), 1)
            backend.enabled = False
            with self.assertRaises(BackendError):
                backend._call("GET", "/key")
            self.assertEqual(len(self.requests), 1)

    def test_shared_reader_stops_at_limit_before_another_stream_chunk(self):
        for vendor in ("everyframe", "runpod"):
            stream = Stream(
                [b"x" * (1024**2 + 1), AssertionError("must not read beyond limit")]
            )
            backend, _ = self.backend(vendor, stream=stream)
            with self.assertRaises(BackendError):
                backend._call("GET", "/key")
            self.assertTrue(stream.closed)
            self.assertEqual(stream.read_chunks, 1)
            self.assertEqual(len(self.requests), 1)

    def test_transport_setup_failure_stays_outside_response_error_mapping(self):
        for vendor in ("everyframe", "runpod"):
            if vendor == "everyframe":
                backend = everyframe.EveryFrameBackend(None, "/unused")
                backend._config = everyframe.Config("test-key")
            else:
                backend = runpod.RunPodEnhanceBackend(None, "/unused")
                backend._config = runpod.Config("test-key", "endpoint1", 970)
            with (
                self.subTest(vendor=vendor),
                patch(
                    "httpx._client.HTTPTransport",
                    side_effect=ValueError("transport-setup"),
                ),
                self.assertRaisesRegex(ValueError, "transport-setup"),
            ):
                backend._call("GET", "/key")
            backend.close()


if __name__ == "__main__":
    unittest.main()
