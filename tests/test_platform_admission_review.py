"""Independent cancellation/download checks against the full, isolated API stack."""
import asyncio
import io
from pathlib import Path
import secrets
import tempfile
import threading
import unittest
from unittest.mock import patch

import httpx

from studio_platform.api import create_app
from studio_platform.http_limits import RequestAdmissionMiddleware, ResponseTimeout
from studio_platform.repository import Scope
from studio_platform.settings import Settings
from test_platform_api import project


class AdmissionReviewTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="sixnine-admission-review-")
        self.addCleanup(self.temp.cleanup)
        self.app = create_app(Settings(Path(self.temp.name), auth_mode="local-test"))
        self.addCleanup(self.app.state.repository.close)
        token = secrets.token_urlsafe(32)
        self.app.state.auth.register_client("review-only", token, "superdan", ["story-one"], ["projects:read", "assets:read"])
        self.headers = {"authorization": "Bearer "+token}
        self.app.state.repository.put_document(Scope("sixnine", "superdan", "__projects"), "project", "story-one", project())

    def scope(self, asset_id="review", method="GET"):
        path = "/v1/assets/"+asset_id+"/content"
        return {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"}, "scheme": "http",
                "http_version": "1.1", "method": method, "path": path, "raw_path": path.encode(),
                "query_string": b"", "root_path": "", "server": ("testserver", 80), "client": ("127.0.0.1", 1234),
                "headers": [(b"host", b"testserver"), (b"authorization", self.headers["authorization"].encode())]}

    def asset(self, key):
        return {"project_id": "story-one", "status": "ready", "mime": "application/octet-stream",
                "file_name": "review.bin", "original": {"key": key}}

    def receiver(self, disconnected=None):
        supplied = False
        async def receive():
            nonlocal supplied
            if not supplied:
                supplied = True
                return {"type": "http.request", "body": b"", "more_body": False}
            await (disconnected or asyncio.Event()).wait()
            return {"type": "http.disconnect"}
        return receive

    async def test_cancelled_sync_handler_does_not_release_allowance_while_thread_still_runs(self):
        admission = self.app.state.request_admission
        admission.limits["total"] = admission.limits["owner"] = 1
        started, release = threading.Event(), threading.Event()
        def held():
            started.set()
            if not release.wait(3):
                raise AssertionError("Synthetic handler release deadline")
            return {"done": True}
        self.app.add_api_route("/v1/review-held", held, methods=["GET"])
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://testserver") as client:
            first = asyncio.create_task(client.get("/v1/review-held", headers=self.headers))
            try:
                self.assertTrue(await asyncio.to_thread(started.wait, 1))
                first.cancel()
                await asyncio.sleep(.025)
                second = await client.get("/v1/projects", headers=self.headers)
                self.assertEqual(second.status_code, 429, "An uncancelled sync operation must keep its concurrency lease")
            finally:
                release.set()
                try:
                    await asyncio.wait_for(first, 2)
                except asyncio.CancelledError:
                    pass
        self.assertFalse(admission.counts)

    async def test_real_asset_stream_file_is_closed_after_full_stack_write_deadline(self):
        for middleware in self.app.user_middleware:
            if middleware.cls is RequestAdmissionMiddleware:
                middleware.kwargs.update(download_seconds=None, send_idle_seconds=None)
        key = "owners/superdan/assets/review/source.bin"
        self.app.state.storage.put(key, io.BytesIO(b"x"*(4*1024*1024)))
        asset = {"project_id": "story-one", "status": "ready", "mime": "application/octet-stream",
                 "file_name": "review.bin", "original": {"key": key}}
        opened, entered = [], asyncio.Event()
        original = self.app.state.storage.open
        def tracked(target):
            handle = original(target)
            opened.append(handle)
            return handle
        scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"}, "scheme": "http",
                 "http_version": "1.1", "method": "GET", "path": "/v1/assets/review/content", "raw_path": b"/v1/assets/review/content",
                 "query_string": b"", "root_path": "", "server": ("testserver", 80), "client": ("127.0.0.1", 1234),
                 "headers": [(b"host", b"testserver"), (b"authorization", self.headers["authorization"].encode())]}
        supplied = False
        async def receive():
            nonlocal supplied
            if not supplied:
                supplied = True
                return {"type": "http.request", "body": b"", "more_body": False}
            await asyncio.Event().wait()
        messages = []
        # Arm the real total deadline only once the file is streaming. Startup
        # latency and a competing idle timer must not choose this test's cause.
        download_deadline = None
        real_timeout = asyncio.timeout
        def capture_deadline(seconds):
            nonlocal download_deadline
            download_deadline = real_timeout(seconds)
            return download_deadline
        async def send(message):
            messages.append(message)
            if message["type"] == "http.response.body" and message.get("body"):
                entered.set()
                download_deadline.reschedule(asyncio.get_running_loop().time())
                await asyncio.Event().wait()
        try:
            with patch("studio_platform.http_limits.asyncio.timeout", side_effect=capture_deadline), patch.object(self.app.state.assets, "get", return_value=asset), patch.object(self.app.state.storage, "open", side_effect=tracked):
                with self.assertRaisesRegex(ResponseTimeout, "media_response_deadline"):
                    await asyncio.wait_for(self.app(scope, receive, send), 5)
            await asyncio.sleep(0)
            self.assertTrue(entered.is_set())
            self.assertEqual(messages[0]["status"], 200)
            self.assertTrue(all(message.get("more_body", False) for message in messages[1:]))
            self.assertTrue(opened)
            self.assertTrue(all(handle.closed for handle in opened), "Timed-out media must not retain open file handles")
            self.assertFalse(self.app.state.request_admission.counts)
        finally:
            for handle in opened:
                handle.close()

    async def test_normal_get_head_and_range_close_only_opened_sources(self):
        key, data = "owners/superdan/assets/review/normal.bin", b"abcdef"*(400*1024)
        self.app.state.storage.put(key, io.BytesIO(data))
        opened, original = [], self.app.state.storage.open
        def tracked(target):
            handle = original(target)
            opened.append(handle)
            return handle
        with patch.object(self.app.state.assets, "get", return_value=self.asset(key)), patch.object(self.app.state.storage, "open", side_effect=tracked):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://testserver") as client:
                full = await client.get("/v1/assets/review/content", headers=self.headers)
                self.assertEqual((full.status_code, full.content), (200, data))
                self.assertTrue(all(handle.closed for handle in opened))
                before = len(opened)
                head = await client.head("/v1/assets/review/content", headers=self.headers)
                self.assertEqual((head.status_code, head.content, int(head.headers["content-length"])), (200, b"", len(data)))
                self.assertEqual(len(opened), before, "HEAD must not open or consume the object")
                partial = await client.get("/v1/assets/review/content", headers={**self.headers, "range": "bytes=12-31"})
                self.assertEqual((partial.status_code, partial.content), (206, data[12:32]))
                self.assertEqual(partial.headers["content-range"], f"bytes 12-31/{len(data)}")
        self.assertTrue(all(handle.closed for handle in opened))
        self.assertFalse(self.app.state.request_admission.counts)

    async def test_disconnect_and_send_failure_close_retained_generator_sources(self):
        key = "owners/superdan/assets/review/connection.bin"
        self.app.state.storage.put(key, io.BytesIO(b"x"*(4*1024*1024)))
        original = self.app.state.storage.open
        for failure in ("disconnect", "send"):
            with self.subTest(failure=failure):
                opened, messages, disconnected = [], [], asyncio.Event()
                def tracked(target):
                    handle = original(target)
                    opened.append(handle)
                    return handle
                async def send(message):
                    messages.append(message)
                    if message["type"] == "http.response.body" and message.get("body"):
                        if failure == "send":
                            raise OSError("synthetic disconnected socket")
                        disconnected.set()
                        # Give the actual StreamingResponse disconnect listener
                        # time to cancel its suspended body iterator.
                        await asyncio.sleep(.025)
                try:
                    with patch.object(self.app.state.assets, "get", return_value=self.asset(key)), patch.object(self.app.state.storage, "open", side_effect=tracked):
                        if failure == "send":
                            with self.assertRaisesRegex(OSError, "synthetic disconnected socket"):
                                await self.app(self.scope(), self.receiver(), send)
                        else:
                            with self.assertRaisesRegex(ResponseTimeout, "incomplete_media_response"):
                                await asyncio.wait_for(self.app(self.scope(), self.receiver(disconnected), send), 2)
                    self.assertEqual(messages[0]["status"], 200)
                    self.assertFalse(any(message["type"] == "http.response.body" and not message.get("more_body", False) for message in messages))
                    self.assertTrue(opened)
                    self.assertTrue(all(handle.closed for handle in opened))
                    self.assertFalse(self.app.state.request_admission.counts)
                finally:
                    for handle in opened:
                        handle.close()

    async def test_storage_read_failure_closes_source_after_partial_response(self):
        key = "owners/superdan/assets/review/read-error.bin"
        self.app.state.storage.put(key, io.BytesIO(b"x"*(4*1024*1024)))
        original, opened, messages = self.app.state.storage.open, [], []
        class BrokenReader:
            def __init__(self, source):
                self.source, self.reads = source, 0
            def __enter__(self):
                return self
            def __exit__(self, *_):
                self.close()
            def seek(self, where):
                return self.source.seek(where)
            def read(self, count):
                self.reads += 1
                if self.reads == 2:
                    raise OSError("synthetic object read failure")
                return self.source.read(count)
            def close(self):
                self.source.close()
            @property
            def closed(self):
                return self.source.closed
        def tracked(target):
            handle = BrokenReader(original(target))
            opened.append(handle)
            return handle
        async def send(message):
            messages.append(message)
        try:
            with patch.object(self.app.state.assets, "get", return_value=self.asset(key)), patch.object(self.app.state.storage, "open", side_effect=tracked):
                with self.assertRaisesRegex(ResponseTimeout, "incomplete_media_response"):
                    await self.app(self.scope(), self.receiver(), send)
            self.assertEqual(messages[0]["status"], 200)
            self.assertTrue(any(message.get("body") for message in messages[1:]))
            self.assertFalse(any(message["type"] == "http.response.body" and not message.get("more_body", False) for message in messages))
            self.assertTrue(opened and all(handle.closed for handle in opened))
            self.assertFalse(self.app.state.request_admission.counts)
        finally:
            for handle in opened:
                handle.close()

    async def test_short_eof_and_oversized_read_never_signal_successful_completion(self):
        key = "owners/superdan/assets/review/inconsistent.bin"
        size = 2*1024*1024
        self.app.state.storage.put(key, io.BytesIO(b"x"*size))
        original = self.app.state.storage.open
        for fault in ("short-eof", "oversized"):
            with self.subTest(fault=fault):
                opened, messages = [], []
                class InconsistentReader:
                    def __init__(self, source):
                        self.source, self.reads = source, 0
                    def __enter__(self):
                        return self
                    def __exit__(self, *_):
                        self.close()
                    def seek(self, where):
                        return self.source.seek(where)
                    def read(self, count):
                        self.reads += 1
                        if fault == "oversized":
                            return self.source.read(size)+b"!"
                        if self.reads == 2:
                            return b""
                        return self.source.read(count)
                    def close(self):
                        self.source.close()
                    @property
                    def closed(self):
                        return self.source.closed
                def tracked(target):
                    handle = InconsistentReader(original(target))
                    opened.append(handle)
                    return handle
                async def send(message):
                    messages.append(message)
                try:
                    with patch.object(self.app.state.assets, "get", return_value=self.asset(key)), patch.object(self.app.state.storage, "open", side_effect=tracked):
                        with self.assertRaisesRegex(ResponseTimeout, "incomplete_media_response"):
                            await self.app(self.scope(), self.receiver(), send)
                    self.assertEqual(messages[0]["status"], 200)
                    bodies = [message for message in messages if message["type"] == "http.response.body"]
                    self.assertTrue(all(message.get("more_body", False) for message in bodies))
                    self.assertLess(sum(len(message.get("body", b"")) for message in bodies), size)
                    self.assertTrue(opened and all(handle.closed for handle in opened))
                    self.assertFalse(self.app.state.request_admission.counts)
                finally:
                    for handle in opened:
                        handle.close()

    async def test_cancelled_download_does_not_close_another_requests_file(self):
        keys = {item: "owners/superdan/assets/review/"+item+".bin" for item in ("first", "second")}
        for key in keys.values():
            self.app.state.storage.put(key, io.BytesIO(b"x"*(4*1024*1024)))
        original, opened = self.app.state.storage.open, {}
        entered = {name: asyncio.Event() for name in keys}
        release = asyncio.Event()
        def tracked(target):
            handle = original(target)
            opened[target] = handle
            return handle
        def get_asset(owner, asset_id):
            self.assertEqual(owner, "superdan")
            return self.asset(keys[asset_id])
        def sender(name):
            async def send(message):
                if message["type"] == "http.response.body" and message.get("body") and not entered[name].is_set():
                    entered[name].set()
                    await release.wait()
            return send
        tasks = []
        try:
            with patch.object(self.app.state.assets, "get", side_effect=get_asset), patch.object(self.app.state.storage, "open", side_effect=tracked):
                tasks = [asyncio.create_task(self.app(self.scope(name), self.receiver(), sender(name))) for name in keys]
                await asyncio.wait_for(asyncio.gather(*(event.wait() for event in entered.values())), 2)
                tasks[0].cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await tasks[0]
                self.assertTrue(opened[keys["first"]].closed)
                self.assertFalse(opened[keys["second"]].closed, "A request lease must not close another request's stream")
                self.assertFalse(tasks[1].done())
                self.assertEqual(self.app.state.request_admission.counts[("downloads", None)], 1)
                release.set()
                await asyncio.wait_for(tasks[1], 2)
            self.assertTrue(all(handle.closed for handle in opened.values()))
            self.assertFalse(self.app.state.request_admission.counts)
        finally:
            release.set()
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            for handle in opened.values():
                handle.close()

    async def test_download_deadline_waits_for_active_read_before_closing_file(self):
        for middleware in self.app.user_middleware:
            if middleware.cls is RequestAdmissionMiddleware:
                middleware.kwargs.update(download_seconds=None, send_idle_seconds=None)
        key = "owners/superdan/assets/review/blocked-read.bin"
        self.app.state.storage.put(key, io.BytesIO(b"x"*(2*1024*1024)))
        original, opened = self.app.state.storage.open, []
        reading, release = threading.Event(), threading.Event()
        class BlockingReader:
            def __init__(self, source):
                self.source, self.closed_during_read = source, False
            def __enter__(self):
                return self
            def __exit__(self, *_):
                self.close()
            def seek(self, where):
                return self.source.seek(where)
            def read(self, count):
                reading.set()
                try:
                    if not release.wait(3):
                        raise AssertionError("Synthetic object read release deadline")
                    return self.source.read(count)
                finally:
                    reading.clear()
            def close(self):
                self.closed_during_read |= reading.is_set()
                self.source.close()
            @property
            def closed(self):
                return self.source.closed
        def tracked(target):
            handle = BlockingReader(original(target))
            opened.append(handle)
            return handle
        async def send(_):
            pass
        # Start the total deadline from the acknowledged active read, not from
        # cold application startup; the independent watchdog bounds the test.
        download_deadline = None
        real_timeout = asyncio.timeout
        def capture_deadline(seconds):
            nonlocal download_deadline
            download_deadline = real_timeout(seconds)
            return download_deadline
        task = None
        try:
            with patch("studio_platform.http_limits.asyncio.timeout", side_effect=capture_deadline), patch.object(self.app.state.assets, "get", return_value=self.asset(key)), patch.object(self.app.state.storage, "open", side_effect=tracked):
                task = asyncio.create_task(asyncio.wait_for(self.app(self.scope(), self.receiver(), send), 5))
                self.assertTrue(await asyncio.to_thread(reading.wait, 3))
                download_deadline.reschedule(asyncio.get_running_loop().time())
                async with real_timeout(1):
                    while not download_deadline.expired():
                        await asyncio.sleep(0)
                self.assertFalse(task.done(), "A timed-out response must await its already-running sync read")
                self.assertTrue(self.app.state.request_admission.counts)
                self.assertTrue(opened and not opened[0].closed)
                release.set()
                with self.assertRaisesRegex(ResponseTimeout, "media_response_deadline"):
                    await asyncio.wait_for(task, 2)
            self.assertTrue(all(handle.closed and not handle.closed_during_read for handle in opened))
            self.assertFalse(self.app.state.request_admission.counts)
        finally:
            release.set()
            if task is not None:
                await asyncio.gather(task, return_exceptions=True)
            for handle in opened:
                handle.close()


if __name__ == "__main__":
    unittest.main()
