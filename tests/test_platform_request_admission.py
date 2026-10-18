"""Real ASGI lifecycle and owner fairness; no network, cloud or paid execution."""
import asyncio
import secrets
import threading
import unittest
from unittest.mock import patch

import httpx
from fastapi import FastAPI, Request
from starlette.responses import StreamingResponse

from studio_platform.http_limits import (BodyLimitMiddleware, RequestAdmission,
                                        RequestAdmissionMiddleware, ResponseTimeout)
import test_platform_api as api_fixtures


class RequestLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_dripping_login_body_expires_and_does_not_hold_all_login_slots(self):
        app = FastAPI()
        entered = asyncio.Event()
        bodies = 0
        async def login(request: Request):
            return {"received": bool(await request.json())}
        app.add_api_route("/api/auth/login", login, methods=["POST"])
        app.add_middleware(BodyLimitMiddleware, project_bytes=1024, upload_bytes=1024,
                           idle_seconds=.05, total_seconds=1, login_seconds=.09)
        admission = RequestAdmission(login=2)
        app.add_middleware(RequestAdmissionMiddleware, admission=admission)
        async def drip():
            nonlocal bodies
            bodies += 1
            if bodies == 2:
                entered.set()
            for _ in range(20):
                await asyncio.sleep(.02)  # Keeps the idle timer alive.
                yield b" "
            yield b"{}"
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            slow = [asyncio.create_task(client.post('/api/auth/login', content=drip(),
                      headers={"content-type": "application/json"})) for _ in range(2)]
            await asyncio.wait_for(entered.wait(), 1)
            self.assertEqual((await client.post('/api/auth/login', json={"username": "fixture"})).status_code, 429)
            self.assertEqual([r.status_code for r in await asyncio.gather(*slow)], [408, 408])
            self.assertEqual((await client.post('/api/auth/login', json={"username": "fixture"})).status_code, 200)
        self.assertFalse(admission.counts)

    async def test_media_total_deadline_stops_even_a_stream_that_keeps_dripping(self):
        admission = RequestAdmission()
        closed = asyncio.Event()
        messages = []
        async def stream(scope, receive, send):
            try:
                await send({"type": "http.response.start", "status": 200, "headers": []})
                for _ in range(50):
                    await asyncio.sleep(.01)
                    await send({"type": "http.response.body", "body": b"x", "more_body": True})
            finally:
                closed.set()
        async def send(message):
            messages.append(message)
        app = RequestAdmissionMiddleware(stream, admission=admission, download_seconds=.07)
        with self.assertRaisesRegex(ResponseTimeout, "media_response_deadline"):
            await app({"type": "http", "method": "GET", "path": "/v1/assets/fixture/content"}, None, send)
        self.assertTrue(closed.is_set())
        self.assertLess(len(messages), 50)
        self.assertFalse(admission.counts)
        self.assertTrue(all(message.get("more_body") for message in messages[1:]))

    async def test_blocked_send_is_aborted_and_releases_global_capacity(self):
        admission = RequestAdmission()
        async def app(scope, receive, send):
            await send({"type": "http.response.start", "status": 200, "headers": []})
        async def blocked_send(message):
            await asyncio.Event().wait()
        wrapped = RequestAdmissionMiddleware(app, admission=admission, send_idle_seconds=.02)
        with self.assertRaisesRegex(ResponseTimeout, "response_write_timeout"):
            await wrapped({"type": "http", "path": "/", "method": "GET"}, None, blocked_send)
        self.assertFalse(admission.counts)

    async def test_stream_headers_do_not_release_capacity_and_cancel_recovers(self):
        admission = RequestAdmission(total=1)
        started = asyncio.Event()
        finish = asyncio.Event()

        async def body(scope, receive, send):
            await send({"type": "http.response.start", "status": 200, "headers": []})
            started.set()
            await finish.wait()
            await send({"type": "http.response.body", "body": b"done"})

        middleware = RequestAdmissionMiddleware(body, admission=admission)

        async def request():
            messages = []
            async def receive():
                return {"type": "http.request", "body": b"", "more_body": False}
            async def send(message):
                messages.append(message)
            await middleware({"type": "http", "path": "/stream", "method": "GET"}, receive, send)
            return messages

        first = asyncio.create_task(request())
        await asyncio.wait_for(started.wait(), 2)
        second = await request()
        self.assertEqual(second[0]["status"], 429)
        self.assertIn((b"retry-after", b"3"), second[0]["headers"])
        first.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first
        self.assertFalse(admission.counts)
        finish.set()
        self.assertEqual((await request())[0]["status"], 200)
        self.assertFalse(admission.counts)

    async def test_response_exception_and_partial_admission_release_every_slot(self):
        admission = RequestAdmission(total=1)
        async def failed(scope, receive, send):
            raise RuntimeError("Synthetic response failure")
        wrapped = RequestAdmissionMiddleware(failed, admission=admission)
        with self.assertRaises(RuntimeError):
            await wrapped({"type": "http", "path": "/"}, None, None)
        self.assertFalse(admission.counts)


class RequestApiAdmissionTests(unittest.TestCase):
    setUp = api_fixtures.ApiTests.setUp
    login = api_fixtures.ApiTests.login
    setup_project = api_fixtures.ApiTests.setup_project

    def machine(self, ident, owner="superdan"):
        token = secrets.token_urlsafe(32)
        self.app.state.auth.register_client(ident, token, owner, ["story-one"], ["projects:read", "assets:read"])
        return {"Authorization": "Bearer " + token}

    def test_two_machine_clients_share_owner_limit_but_other_owner_progresses(self):
        self.setup_project()
        self.login("supervan")
        self.client.post("/v1/projects", json={"project": api_fixtures.project()}).raise_for_status()
        first_headers, second_headers = self.machine("one"), self.machine("two")
        other_headers = self.machine("other", "supervan")
        self.app.state.request_admission.limits["owner"] = 1
        entered, release = threading.Event(), threading.Event()
        original = self.app.state.repository.get_document

        def held(scope, *args, **kwargs):
            if scope.owner_id == "superdan":
                entered.set()
                if not release.wait(5):
                    raise AssertionError("Request test timed out")
            return original(scope, *args, **kwargs)

        async def scenario():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://testserver") as client:
                first = asyncio.create_task(client.get("/v1/projects/story-one", headers=first_headers))
                try:
                    self.assertTrue(await asyncio.to_thread(entered.wait, 3))
                    same_owner = await client.get("/v1/projects/story-one", headers=second_headers)
                    self.assertEqual(same_owner.status_code, 429)
                    self.assertEqual(same_owner.headers["Retry-After"], "3")
                    other = await client.get("/v1/projects/story-one", headers=other_headers)
                    self.assertEqual(other.status_code, 200)
                finally:
                    release.set()
                    self.assertEqual((await first).status_code, 200)
                self.assertEqual((await client.get("/v1/projects/story-one", headers=second_headers)).status_code, 200)
        with patch.object(self.app.state.repository, "get_document", side_effect=held):
            asyncio.run(scenario())
        self.assertFalse(self.app.state.request_admission.counts)

    def test_authentication_gate_rejects_before_database_or_threadpool_work(self):
        self.setup_project()
        headers = self.machine("auth-test")
        self.app.state.request_admission.limits["authentication"] = 1
        entered, release = threading.Event(), threading.Event()
        original = self.app.state.auth.bearer
        calls = []
        def held(token):
            calls.append(True)
            entered.set()
            if not release.wait(5):
                raise AssertionError("Authentication test timed out")
            return original(token)
        async def scenario():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://testserver") as client:
                first = asyncio.create_task(client.get("/v1/projects", headers=headers))
                try:
                    self.assertTrue(await asyncio.to_thread(entered.wait, 3))
                    second = await client.get("/v1/projects", headers=headers)
                    self.assertEqual(second.status_code, 429)
                    self.assertEqual(len(calls), 1)
                finally:
                    release.set()
                    self.assertEqual((await first).status_code, 200)
        with patch.object(self.app.state.auth, "bearer", side_effect=held):
            asyncio.run(scenario())
        self.assertFalse(self.app.state.request_admission.counts)

    def test_download_slot_lasts_past_headers_and_frees_after_body(self):
        self.setup_project()
        headers = self.machine("download-test")
        self.app.state.request_admission.limits["owner_downloads"] = 1

        async def scenario():
            entered, release = asyncio.Event(), asyncio.Event()
            async def stream():
                entered.set()
                yield b"first"
                await release.wait()
                yield b"last"
            # An isolated streamed route exercises the real auth/middleware;
            # production artifact ownership is separately tested by API tests.
            async def endpoint():
                return StreamingResponse(stream())
            self.app.add_api_route("/v1/assets/test/slow/content", endpoint, methods=["GET"])
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://testserver") as client:
                first = asyncio.create_task(client.get("/v1/assets/test/slow/content", headers=headers))
                try:
                    await asyncio.wait_for(entered.wait(), 3)
                    second = await client.get("/v1/assets/test/slow/content", headers=headers)
                    self.assertEqual(second.status_code, 429)
                    ordinary = await client.get("/v1/projects", headers=headers)
                    self.assertEqual(ordinary.status_code, 200)
                finally:
                    release.set()
                    result = await first
                    self.assertEqual((result.status_code, result.content), (200, b"firstlast"))
                again = await client.get("/v1/assets/test/slow/content", headers=headers)
                self.assertEqual(again.status_code, 200)
        asyncio.run(scenario())
        self.assertFalse(self.app.state.request_admission.counts)

    def test_login_cpu_gate_returns_retry_without_entering_password_check(self):
        self.app.state.request_admission.limits["login"] = 1
        entered, release = threading.Event(), threading.Event()
        original = self.app.state.auth.login
        calls = []
        def held(*args, **kwargs):
            calls.append(True)
            entered.set()
            if not release.wait(5):
                raise AssertionError("Login test timed out")
            return original(*args, **kwargs)
        async def scenario():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://testserver") as client:
                first = asyncio.create_task(client.post("/api/auth/login", json={"username": "superdan"}))
                try:
                    self.assertTrue(await asyncio.to_thread(entered.wait, 3))
                    second = await client.post("/api/auth/login", json={"username": "supervan"})
                    self.assertEqual(second.status_code, 429)
                    self.assertEqual(len(calls), 1)
                finally:
                    release.set()
                    self.assertEqual((await first).status_code, 200)
        with patch.object(self.app.state.auth, "login", side_effect=held):
            asyncio.run(scenario())
        self.assertFalse(self.app.state.request_admission.counts)


if __name__ == "__main__":
    unittest.main()
