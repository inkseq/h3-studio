"""Injected transports/synthetic credentials only. Never requests Boyesir.

Ledger boundary tests use LedgerCase's temporary SQLite or explicit local PG
schema. They exercise the existing TaskQueue submission intent, not a new DB.
"""
from concurrent.futures import ThreadPoolExecutor
import copy
from dataclasses import replace
import json
import logging
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

import httpx

from studio_platform.boyesir_backend import (
    ApprovedMedia, BASE_URL, PROFILE, SERVICE, BoyesirBackend, SubmissionRecord,
    validate_request, _binding,
)
from studio_platform.worker import BackendError, NotReady, SubmissionUncertain, SubmissionRejected
from studio_platform.queue import TaskQueue
from studio_platform.repository import InvalidTransition
from test_platform_repository import LedgerCase


CANARY = "synthetic-boyesir-secret-never-log"
URL_CANARY = "synthetic-signed-query-never-persist"


def request(model="bh-minimax-h3-pro-768p", media=None):
    return {"recipe_id": "boyesir-video-v1", "provider_request": {
        "model": model, "prompt": "A synthetic offline test", "duration": 8,
        "resolution": "2k" if model == "bh-hailuo-h3-2k" else "768p",
        "ratio": "provider_default", "output_audio": "provider_default", "media": media or []}}


def job(media=None):
    return {"id": "job-1", "tenant_id": "test-tenant", "owner_id": "superdan", "project_id": "project-1", "request": request(media=media)}


def credentials(service, *, profile):
    assert (service, profile) == (SERVICE, PROFILE)
    return SimpleNamespace(service=SERVICE, profile=PROFILE, base_url=BASE_URL, api_key=CANARY)


class FakeAtomicGate:
    """Testing only. Production must inject the durable shared-ledger equivalent."""
    def __init__(self):
        self.rows = {}
        self.lock = threading.Lock()

    def consume(self, binding):
        with self.lock:
            if binding.tag in self.rows:
                return False
            self.rows[binding.tag] = SubmissionRecord(binding, None)
            return True

    def record_accepted(self, binding, task_id):
        with self.lock:
            if self.rows[binding.tag].binding != binding:
                raise RuntimeError("fake mismatch")
            self.rows[binding.tag] = SubmissionRecord(binding, task_id)

    def lookup(self, tag):
        return self.rows.get(tag)


class Resolver:
    def __init__(self, change=None):
        self.change = change or {}

    def resolve(self, **kw):
        return replace(ApprovedMedia(**kw, sha256="a"*64, expires_at=5000,
            url="https://assets.example.test/private/file?signature="+URL_CANARY), **self.change)


class BackendTests(unittest.TestCase):
    def setUp(self):
        self.gate = FakeAtomicGate()
        self.calls = []
        self.state = "processing"
        self.result_url = "https://gf.boyesir.com/assets/output/test.mp4?signature="+URL_CANARY

    def handle(self, req):
        self.calls.append(req)
        self.assertEqual(req.headers["authorization"], "Bearer "+CANARY)
        if req.method == "POST":
            return httpx.Response(200, json={"task_id": "canvas_vid_offline", "status": "queued"})
        return httpx.Response(200, json={"status": self.state, "error": CANARY,
            "result": {"videos": [self.result_url]}})

    def backend(self, **kw):
        args = dict(enabled=True, submit_gate=self.gate, credential_loader=credentials,
            transport=httpx.MockTransport(self.handle), clock=lambda: 1000,
            input_hosts=("assets.example.test",), media_resolver=Resolver())
        args.update(kw)
        backend = BoyesirBackend(**args)
        self.addCleanup(backend.close)
        return backend

    def submitted(self, **kw):
        backend = self.backend(**kw)
        prepared = backend.prepare(job(), "attempt", None, lambda: None)
        self.assertEqual(backend.submit(prepared, "attempt"), "canvas_vid_offline")
        return backend

    def test_disabled_or_missing_gate_never_loads_credentials_or_transport(self):
        loader = Mock(side_effect=AssertionError("must not load"))
        for backend in (BoyesirBackend(credential_loader=loader), BoyesirBackend(enabled=True, credential_loader=loader)):
            with self.assertRaises(NotReady):
                backend.prepare(job(), "attempt", None, lambda: None)
        loader.assert_not_called()

    def test_profile_and_base_url_mismatch_fail_before_intent(self):
        for changes in ({"base_url": None}, {"base_url": BASE_URL+"/v1"}, {"base_url": "http://boyesir.com"},
                        {"profile": "other"}, {"service": "openai"}, {"api_key": "bad\nheader"}):
            def loader(*args, **kw):
                values = vars(credentials(SERVICE, profile=PROFILE)).copy()
                values.update(changes)
                return SimpleNamespace(**values)
            with self.subTest(changes=tuple(changes)):
                with self.assertRaises(NotReady) as ctx:
                    self.backend(credential_loader=loader).prepare(job(), "attempt", None, lambda: None)
                self.assertNotIn(CANARY, str(ctx.exception))
        self.assertFalse(self.calls)
        self.assertFalse(self.gate.rows)

    def test_explicit_defaults_and_distinct_model_contracts(self):
        self.assertFalse(BoyesirBackend.capabilities()["online_verified"])
        self.assertIsNone(BoyesirBackend.capabilities()["exact_pixel_dimensions"])
        for model in ("bh-minimax-h3-pro-768p", "bh-hailuo-h3-2k"):
            self.assertEqual(validate_request(request(model))["model"], model)
        bad = [dict(seed=1), dict(first_frame_url="https://example.test/a"), dict(generate_audio=True),
               dict(ratio="16:9"), dict(output_audio=False), dict(width=1024), dict(steps=50), dict(duration=True),
               dict(duration=16), dict(model="lec-minimax-h3-768p"), dict(model="minimax-h3-768p")]
        for change in bad:
            value = request()
            value["provider_request"].update(change)
            with self.subTest(change=change), self.assertRaises(BackendError):
                validate_request(value)
        with self.assertRaises(BackendError):
            validate_request({"request": {"duration": 8, "generate_audio": False}})

    def test_media_limits_and_audio_visual_requirement(self):
        media = lambda n, kind: [{"asset_id": kind+str(i), "kind": kind, "sha256": "a"*64} for i in range(n)]
        for items in (media(10, "image"), media(1, "video"), media(1, "audio"), media(1, "image")+media(4, "audio")):
            with self.assertRaises(BackendError):
                validate_request(request(media=items))
        body = request("bh-hailuo-h3-2k", media(9, "image")+media(3, "video")+media(3, "audio"))
        self.assertEqual(len(validate_request(body)["media"]), 15)

    def test_only_same_owner_project_model_and_sha_approved_urls_are_sent(self):
        media = [{"asset_id": "image-1", "kind": "image", "sha256": "a"*64}]
        for change in ({"owner_id": "supervan"}, {"tenant_id": "other"}, {"project_id": "other"},
                       {"model_id": "other"}, {"sha256": "b"*64}, {"expires_at": 1100},
                       {"url": "https://unapproved.example.test/a"}, {"url": "http://assets.example.test/a"}):
            with self.subTest(change=tuple(change)), self.assertRaises(BackendError):
                self.backend(media_resolver=Resolver(change)).prepare(job(media), "attempt", None, lambda: None)
        backend = self.backend()
        prepared = backend.prepare(job(media), "attempt", None, lambda: None)
        self.assertNotIn(URL_CANARY, repr(prepared))
        self.assertNotIn(URL_CANARY, repr(Resolver().resolve(tenant_id="t", owner_id="o", project_id="p",
            asset_id="a", kind="image", model_id="m")))
        backend.submit(prepared, "attempt")
        payload = json.loads(self.calls[0].content)
        self.assertEqual(set(payload), {"model", "prompt", "duration", "resolution", "images"})
        self.assertEqual(payload["images"], ["https://assets.example.test/private/file?signature="+URL_CANARY])
        self.assertNotIn(URL_CANARY, repr(self.gate.rows))
        self.assertNotIn("A synthetic offline test", repr(self.gate.rows))

    def test_parallel_attempts_consume_once_and_restarted_adapter_does_not_post_again(self):
        first, second = self.backend(), self.backend()
        def submit(backend):
            try:
                return backend.submit(backend.prepare(job(), "attempt", None, lambda: None), "attempt")
            except SubmissionUncertain:
                return "blocked"
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(submit, (first, second)))
        self.assertEqual(sorted(results), ["blocked", "canvas_vid_offline"])
        self.assertEqual(sum(x.method == "POST" for x in self.calls), 1)
        fresh = self.backend()
        self.assertEqual(fresh.reconcile("attempt").state, "running")
        with self.assertRaises(SubmissionUncertain):
            fresh.submit(fresh.prepare(job(), "attempt", None, lambda: None), "attempt")

    def test_post_timeout_unknown_has_no_task_id_no_query_and_no_retry(self):
        def timeout(req):
            self.calls.append(req)
            raise httpx.ReadTimeout("unsafe "+CANARY+URL_CANARY)
        backend = self.backend(transport=httpx.MockTransport(timeout))
        prepared = backend.prepare(job(), "attempt", None, lambda: None)
        with self.assertRaises(SubmissionUncertain) as ctx:
            backend.submit(prepared, "attempt")
        self.assertNotIn(CANARY, str(ctx.exception))
        self.assertEqual(backend.reconcile("attempt").state, "unknown")
        with self.assertRaises(SubmissionUncertain):
            other = self.backend()
            other.submit(other.prepare(job(), "attempt", None, lambda: None), "attempt")
        self.assertEqual(len(self.calls), 1)

    def test_prepared_body_binding_and_expiry_are_sealed_before_gate(self):
        backend = self.backend()
        prepared = backend.prepare(job(), "attempt", None, lambda: None)
        for changed in (replace(prepared, body=b'{"model":"unapproved","images":["https://evil.test/a"]}'),
                        replace(prepared, binding=replace(prepared.binding, owner_id="supervan")),
                        replace(prepared, expires_at=prepared.expires_at+3600),
                        replace(prepared, seal=b"0"*32)):
            with self.assertRaises(BackendError):
                backend.submit(changed, "attempt")
        self.assertFalse(self.gate.rows)
        self.assertFalse(self.calls)
        with self.assertRaises(BackendError):
            self.backend().submit(prepared, "attempt")
        backend.submit(prepared, "attempt")
        self.assertEqual(len(self.calls), 1)

    def test_error_response_or_task_id_persistence_failure_never_blind_retries(self):
        for status in (302, 400, 401, 429, 500):
            gate = FakeAtomicGate()
            backend = self.backend(submit_gate=gate, transport=httpx.MockTransport(lambda r: httpx.Response(status)))
            prepared = backend.prepare(job(), "attempt", None, lambda: None)
            with self.subTest(status=status), self.assertRaises(SubmissionUncertain):
                backend.submit(prepared, "attempt")
            self.assertIsNone(gate.lookup("attempt").task_id)
        gate = FakeAtomicGate()
        gate.record_accepted = Mock(side_effect=RuntimeError("ledger lost response "+CANARY))
        backend = self.backend(submit_gate=gate)
        prepared = backend.prepare(job(), "attempt", None, lambda: None)
        with self.assertRaises(SubmissionUncertain):
            backend.submit(prepared, "attempt")
        with self.assertRaises(SubmissionUncertain):
            backend.submit(prepared, "attempt")

    def test_malformed_or_oversized_create_response_and_wrong_gate_never_authorize_retry(self):
        for response in (httpx.Response(200, content=b"not-json"), httpx.Response(200, json={"task_id": "../escape"}),
                         httpx.Response(200, json={"status": "queued"}), httpx.Response(200, content=b"x"*(1024**2+1))):
            gate = FakeAtomicGate()
            backend = self.backend(submit_gate=gate, transport=httpx.MockTransport(lambda r: response))
            prepared = backend.prepare(job(), "attempt", None, lambda: None)
            with self.assertRaises(SubmissionUncertain):
                backend.submit(prepared, "attempt")
            self.assertIsNone(gate.lookup("attempt").task_id)
        gate = FakeAtomicGate()
        gate.consume = Mock(side_effect=RuntimeError("ambiguous commit "+CANARY))
        backend = self.backend(submit_gate=gate)
        with self.assertRaises(SubmissionUncertain):
            backend.submit(backend.prepare(job(), "attempt", None, lambda: None), "attempt")
        self.assertFalse(self.calls)

    def test_only_documented_402_is_definite_uncharged_rejection(self):
        backend = self.backend(transport=httpx.MockTransport(lambda r: httpx.Response(402)))
        prepared = backend.prepare(job(), "attempt", None, lambda: None)
        with self.assertRaises(SubmissionRejected):
            backend.submit(prepared, "attempt")
        with self.assertRaises(SubmissionUncertain):
            backend.submit(prepared, "attempt")

    def test_poll_cancel_and_unknown_status_never_claim_refund_or_stop(self):
        backend = self.submitted()
        for state, expected in (("queued", "running"), ("processing", "running"), ("succeeded", "succeeded"),
                                ("failed", "failed"), ("refunded", "unknown")):
            self.state = state
            result = backend.poll("attempt", "canvas_vid_offline")
            self.assertEqual(result.state, expected)
            self.assertIsNone(result.actual_cost_microusd)
            self.assertNotIn(CANARY, repr(result))
        count = len(self.calls)
        self.assertFalse(backend.cancel("attempt", "canvas_vid_offline"))
        self.assertEqual(len(self.calls), count)
        with self.assertRaises(BackendError):
            backend.poll("attempt", "another-owner-task")

    def test_poll_rejects_conflicting_echo_and_handles_malformed_status(self):
        backend = self.submitted()
        backend._transport = httpx.MockTransport(lambda r: httpx.Response(200,
            json={"task_id": "different-task", "status": "succeeded"}))
        with self.assertRaises(BackendError):
            backend.poll("attempt", "canvas_vid_offline")
        backend._transport = httpx.MockTransport(lambda r: httpx.Response(200, json={"status": ["succeeded"]}))
        self.assertEqual(backend.poll("attempt", "canvas_vid_offline").state, "unknown")

    def test_real_httpcore_trace_redacts_private_headers_only_in_this_context(self):
        from httpcore._trace import Trace
        def traced(req):
            with Trace("receive_response_headers", logging.getLogger("httpcore.http11")) as trace:
                trace.return_value = (b"HTTP/1.1", 302, b"Found", [(b"Location", self.result_url.encode()),
                    (b"Set-Cookie", CANARY.encode())])
            return self.handle(req)
        backend = self.backend(transport=httpx.MockTransport(traced))
        with self.assertLogs("httpcore", level="DEBUG") as logs:
            backend.submit(backend.prepare(job(), "attempt", None, lambda: None), "attempt")
            logging.getLogger("httpcore.http11").debug("other-project-marker")
        output = " ".join(logs.output)
        self.assertNotIn(CANARY, output)
        self.assertNotIn(URL_CANARY, output)
        self.assertIn("private details redacted", output)
        self.assertIn("other-project-marker", output)

    def test_download_is_public_https_without_authorization_cookie_or_url_log(self):
        downloads = []
        def download(req):
            downloads.append(req)
            self.assertNotIn("authorization", req.headers)
            self.assertNotIn("cookie", req.headers)
            return httpx.Response(200, content=b"synthetic-video-bytes", headers={"Content-Length": "21"})
        backend = self.submitted(download_transport=httpx.MockTransport(download))
        self.state = "succeeded"
        with tempfile.TemporaryDirectory() as tmp, self.assertLogs("httpx", level="INFO") as logs:
            logging.getLogger("httpx").info("synthetic test marker")
            paths = backend.fetch(job(), "attempt", "canvas_vid_offline", Path(tmp), lambda: None)
            self.assertEqual(paths["video"].read_bytes(), b"synthetic-video-bytes")
            self.assertEqual(set(paths), {"video"})
            backend.fetch(job(), "attempt", "canvas_vid_offline", Path(tmp), lambda: None)
        self.assertEqual(len(downloads), 2)
        self.assertNotIn(URL_CANARY, " ".join(logs.output))
        self.assertNotIn(CANARY, repr(backend))

    def test_download_url_redirect_size_and_encoding_boundaries(self):
        download = Mock(return_value=httpx.Response(200, content=b"x"))
        backend = self.submitted(download_transport=httpx.MockTransport(download), max_download_bytes=10)
        self.state = "succeeded"
        with tempfile.TemporaryDirectory() as tmp:
            for bad in ("http://gf.boyesir.com/a", "https://gf.boyesir.com.evil.test/a", "https://evil.test/a",
                        "https://gf.boyesir.com:444/a", "https://user@gf.boyesir.com/a", "https://gf.boyesir.com/a#b",
                        "https://gf.boyesir.com\\@evil.test/a", "https://127.0.0.1/a"):
                self.result_url = bad
                with self.subTest(url=bad), self.assertRaises(BackendError):
                    backend.fetch(job(), "attempt", "canvas_vid_offline", Path(tmp), lambda: None)
            download.assert_not_called()
            self.result_url = "https://hub.boyesir.com/a?signature="+URL_CANARY
            for response in (httpx.Response(302, headers={"Location": "https://evil.test/a"}),
                             httpx.Response(200, content=b"x"*11),
                             httpx.Response(200, content=b"x", headers={"Content-Length": "9999999999999999"}),
                             httpx.Response(200, content=b"x", headers={"Content-Encoding": "arbitrary"})):
                download.return_value = response
                with self.assertRaises(BackendError):
                    backend.fetch(job(), "attempt", "canvas_vid_offline", Path(tmp), lambda: None)
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_download_owner_and_hardlink_targets_rejected(self):
        backend = self.submitted(download_transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b"x")))
        self.state = "succeeded"
        other = job()
        other["owner_id"] = "supervan"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaises(BackendError):
                backend.fetch(other, "attempt", "canvas_vid_offline", root, lambda: None)
            source = root/"unique-source"
            source.write_bytes(b"keep")
            (root/"boyesir-raw.mp4").hardlink_to(source)
            with self.assertRaises(BackendError):
                backend.fetch(job(), "attempt", "canvas_vid_offline", root, lambda: None)
            self.assertEqual(source.read_bytes(), b"keep")

    def test_download_slow_stream_or_lost_heartbeat_is_bounded_and_private(self):
        ticks = [0]
        class Slow(httpx.SyncByteStream):
            def __iter__(self):
                yield b"a"
                ticks[0] = 200
                yield b"b"
        backend = self.submitted(download_transport=httpx.MockTransport(lambda r: httpx.Response(200, stream=Slow())),
                                 monotonic=lambda: ticks[0])
        self.state = "succeeded"
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(BackendError) as ctx:
                backend.fetch(job(), "attempt", "canvas_vid_offline", Path(tmp), lambda: None)
            self.assertNotIn(URL_CANARY, str(ctx.exception))
            self.assertEqual(list(Path(tmp).iterdir()), [])
        # A heartbeat failure propagates only a stable adapter error, with no raw URL.
        beat = Mock(side_effect=[None, RuntimeError("lease lost "+URL_CANARY)])
        ticks[0] = 0
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(BackendError) as ctx:
                backend.fetch(job(), "attempt", "canvas_vid_offline", Path(tmp), beat)
            self.assertNotIn(URL_CANARY, str(ctx.exception))
            self.assertEqual(list(Path(tmp).iterdir()), [])


class ExistingQueueGate:
    """Test integration only: replaces (does not duplicate) runner intent commit."""
    def __init__(self, queue, lease, expected):
        self.queue, self.lease, self.expected = queue, lease, expected

    def consume(self, binding):
        if binding != self.expected:
            return False
        try:
            self.queue.begin_submission(self.lease)
            return True
        except InvalidTransition:
            return False

    def record_accepted(self, binding, task_id):
        if binding != self.expected:
            raise RuntimeError("binding mismatch")
        self.queue.record_submitted(self.lease, task_id)

    def lookup(self, tag):
        if tag != self.expected.tag:
            return None
        from studio_platform.repository import attempts
        from sqlalchemy import select
        with self.queue.repository.engine.connect() as conn:
            row = conn.execute(select(attempts).where(attempts.c.id == tag)).mappings().one()
        return SubmissionRecord(self.expected, row["upstream_task_id"]) if row["submission_started_at"] is not None else None


class ExistingLedgerBoundaryTests(LedgerCase):
    def test_real_ledger_intent_blocks_second_process_after_unknown_post(self):
        plan = self.repo.create_plan(self.scope, request(), {"pool": "test-pool", "backend": "boyesir-api"},
            expires_at=self.now+1000, estimated_cost_microusd=100)
        row = self.repo.create_job(self.scope, plan["id"], "boyesir-test", budget_account_ids=["owner-budget"])
        queue = TaskQueue(self.repo)
        lease = queue.claim("api-worker", "test-pool").lease
        binding, _ = _binding(row, lease.attempt_id)
        calls = []
        def timeout(req):
            calls.append(req.method)
            raise httpx.ReadTimeout("synthetic unknown response")
        first = BoyesirBackend(enabled=True, submit_gate=ExistingQueueGate(queue, lease, binding),
            credential_loader=credentials, transport=httpx.MockTransport(timeout), clock=lambda: self.now)
        second = BoyesirBackend(enabled=True, submit_gate=ExistingQueueGate(TaskQueue(self.repo), lease, binding),
            credential_loader=credentials, transport=httpx.MockTransport(timeout), clock=lambda: self.now)
        self.addCleanup(first.close)
        self.addCleanup(second.close)
        def submit(backend):
            try:
                backend.submit(backend.prepare(row, lease.attempt_id, None, lambda: None), lease.attempt_id)
                return "unexpected_success"
            except SubmissionUncertain:
                return "unknown_or_already_consumed"
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(list(pool.map(submit, (first, second))), ["unknown_or_already_consumed"]*2)
        self.assertEqual(calls, ["POST"])
        self.assertEqual(self.repo.get_job(self.scope, row["id"])["status"], "submitting")
        queue.mark_submission_unknown(lease)
        self.assertEqual(second.reconcile(lease.attempt_id).state, "unknown")
        self.assertIsNone(queue.claim("new-worker", "test-pool", purpose="generate"))
        from studio_platform.repository import budget_accounts
        from sqlalchemy import select
        with self.repo.engine.connect() as conn:
            self.assertEqual(conn.execute(select(budget_accounts.c.reserved_microusd)
                .where(budget_accounts.c.id == "owner-budget")).scalar_one(), 100)


if __name__ == "__main__":
    unittest.main()
