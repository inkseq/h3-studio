"""Tiny multipart contract/cleanup cases against a temporary API database."""
import asyncio
from contextlib import contextmanager
import threading
import unittest
from unittest import mock

import anyio
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.openapi.utils import get_openapi
from fastapi.routing import APIRoute
from starlette import formparsers
from starlette.requests import Request

from studio_platform.upload_route import AssetUploadRoute, _handle_upload
import test_platform_api as fixtures

png = fixtures.png


@contextmanager
def tracked_spools(*, force_disk=False):
    opened = []
    real = formparsers.SpooledTemporaryFile

    def create(*args, **kwargs):
        if force_disk:
            kwargs["max_size"] = 1
        value = real(*args, **kwargs)
        opened.append(value)
        return value

    with mock.patch.object(formparsers, "SpooledTemporaryFile", side_effect=create):
        try:
            yield opened
        finally:
            # Test cleanup even when an assertion fails; only these test files.
            for file in opened:
                file.close()


def multipart(parts, boundary="small-test"):
    data = bytearray()
    for name, filename, body in parts:
        data.extend(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"'.encode())
        if filename is not None:
            data.extend(f'; filename="{filename}"'.encode())
        data.extend(b"\r\n\r\n" + body + b"\r\n")
    data.extend(f"--{boundary}--\r\n".encode())
    return bytes(data)


class UploadRouteHttpTests(unittest.TestCase):
    def setUp(self):
        # Reuse production API/auth/storage dependencies, always temporary data.
        self.case = fixtures.ApiTests("test_auth_required_and_bad_host_origin")
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.case.setup_project()
        self.app, self.client = self.case.app, self.case.client
        index, original = next((index, route) for index, route in enumerate(self.app.router.routes)
                               if getattr(route, "path", None) == "/v1/assets" and "POST" in route.methods)
        self.assertIsInstance(original, AssetUploadRoute, 'production upload route must use strict parser')
        baseline = APIRoute(original.path, original.endpoint,
            methods=original.methods, status_code=original.status_code, name=original.name)
        self.original_schema = get_openapi(title='test', version='test', routes=[baseline])
        self.strict = original

    def upload(self, fields):
        with tracked_spools() as files:
            response = self.client.post("/v1/assets", files=fields)
            self.assertTrue(all(file.closed for file in files), "route must close completed or rejected forms")
            return response, len(files)

    def test_valid_upload_preserves_schema_status_and_single_parse(self):
        response, count = self.upload([("file", ("reference.png", png(), "image/png")),
            ("client_project_id", (None, "story-one")), ("client_asset_id", (None, "local:media"))])
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(count, 1)
        self.assertEqual(response.json()["status"], "ready")
        current = get_openapi(title='test', version='test', routes=[self.strict])
        before = self.original_schema["paths"]["/v1/assets"]["post"]["requestBody"]
        after = current["paths"]["/v1/assets"]["post"]["requestBody"]
        self.assertEqual(before, after)
        name = before["content"]["multipart/form-data"]["schema"]["$ref"].split("/")[-1]
        self.assertEqual(self.original_schema["components"]["schemas"][name], current["components"]["schemas"][name])

    def test_extra_and_duplicate_files_rejected_before_second_spool(self):
        for name in ("extra", "file"):
            with self.subTest(name=name):
                response, count = self.upload([("file", ("first.png", png(), "image/png")),
                    (name, ("second.png", png(), "image/png")), ("client_project_id", (None, "story-one"))])
                self.assertEqual(response.status_code, 400, response.text)
                self.assertEqual(count, 1)
        self.assertEqual(self.app.state.assets.list("superdan", "story-one"), [])

    def test_duplicate_project_and_optional_identifier_are_not_silently_last_wins(self):
        for name in ("client_project_id", "client_asset_id"):
            fields = [("file", ("reference.png", png(), "image/png")),
                ("client_project_id", (None, "story-one"))]
            if name == "client_asset_id":
                fields.append((name, (None, "first-id")))
            fields.append((name, (None, "story-one" if name == "client_project_id" else "second-id")))
            response, _ = self.upload(fields)
            self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(self.app.state.assets.list("superdan", "story-one"), [])

    def test_unknown_field_or_only_unknown_file_cannot_reach_asset_service(self):
        for fields in ([('file', ('reference.png', png(), 'image/png')),
                        ('client_project_id', (None, 'story-one')), ('unexpected', (None, 'small'))],
                       [('unexpected', ('reference.png', png(), 'image/png')),
                        ('client_project_id', (None, 'story-one'))]):
            response, _ = self.upload(fields)
            self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(self.app.state.assets.list("superdan", "story-one"), [])

    def test_text_part_limit_not_file_payload_limit(self):
        response, count = self.upload([('file', ('reference.png', png(), 'image/png')),
            ('client_project_id', (None, 'story-one')), ('client_asset_id', (None, 'x' * 4097))])
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(count, 1)
        # File content may exceed 4096 bytes; total byte limits are independent.
        from PIL import Image
        import io
        image = Image.new('RGB', (512, 512)); image.putdata([(i % 256, i // 256 % 256, i * 17 % 256) for i in range(512*512)])
        data = io.BytesIO(); image.save(data, 'PNG', compress_level=0)
        response, _ = self.upload([('file', ('reference.png', data.getvalue(), 'image/png')),
            ('client_project_id', (None, 'story-one'))])
        self.assertGreater(len(data.getvalue()), 4096)
        self.assertEqual(response.status_code, 201, response.text)

    def test_missing_required_fields_keep_fastapi_422_and_close_file(self):
        for fields in ([('file', ('reference.png', png(), 'image/png'))],
                       [('client_project_id', (None, 'story-one'))]):
            response, _ = self.upload(fields)
            self.assertEqual(response.status_code, 422, response.text)

    def test_pinned_parser_rejects_32k_filename_before_any_spool(self):
        response, count = self.upload([('file', ('x' * 32768 + '.png', png(), 'image/png')),
            ('client_project_id', (None, 'story-one'))])
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(count, 0)
        self.assertNotIn('x' * 50, response.text)

    def test_authentication_still_runs_before_route_body_parse(self):
        self.client.post('/api/auth/logout').raise_for_status()
        response, count = self.upload([('file', ('reference.png', png(), 'image/png')),
            ('client_project_id', (None, 'story-one'))])
        self.assertEqual(response.status_code, 401)
        self.assertEqual(count, 0)

    def test_truncated_normal_eof_returns_400_and_closes_unfinished_file(self):
        body = b'--small-test\r\nContent-Disposition: form-data; name="file"; filename="ref.png"\r\n\r\nsmall'
        with tracked_spools(force_disk=True) as files:
            response = self.client.post('/v1/assets', content=body,
                headers={'Content-Type': 'multipart/form-data; boundary=small-test'})
            self.assertEqual(response.status_code, 400, response.text)
            self.assertEqual(len(files), 1)
            self.assertTrue(files[0].closed)
        self.assertEqual(self.app.state.assets.list('superdan', 'story-one'), [])

    def test_complete_multipart_with_epilogue_is_not_rejected(self):
        body = multipart([('file', 'reference.png', png()), ('client_project_id', None, b'story-one')])
        with tracked_spools() as files:
            response = self.client.post('/v1/assets', content=body + b'valid epilogue\r\n',
                headers={'Content-Type': 'multipart/form-data; boundary=small-test'})
            self.assertEqual(response.status_code, 201, response.text)
            self.assertEqual(len(files), 1)
            self.assertTrue(files[0].closed)

    def test_other_content_types_are_415_not_form_parsed(self):
        for content_type in ('application/x-www-form-urlencoded', 'application/json', 'text/plain'):
            with self.subTest(content_type=content_type), tracked_spools() as files:
                response = self.client.post('/v1/assets', content=b'client_project_id=story-one',
                    headers={'Content-Type': content_type})
                self.assertEqual(response.status_code, 415, response.text)
                self.assertEqual(len(files), 0)


class UploadRouteCleanupTests(unittest.IsolatedAsyncioTestCase):
    def request(self, body, *, receive_error=None):
        count = 0

        async def receive():
            nonlocal count
            count += 1
            if count == 1:
                return {'type': 'http.request', 'body': body, 'more_body': receive_error is not None}
            raise receive_error

        return Request({'type': 'http', 'method': 'POST', 'path': '/v1/assets',
            'headers': [(b'content-type', b'multipart/form-data; boundary=small-test')], 'app': object()}, receive)

    async def test_endpoint_error_and_cancellation_close_disk_spooled_form(self):
        body = multipart([('file', 'reference.png', png()), ('client_project_id', None, b'story-one')])
        for exception in (HTTPException(422, 'test endpoint error'), asyncio.CancelledError()):
            async def endpoint(request):
                raise exception
            with tracked_spools(force_disk=True) as files:
                with self.assertRaises(type(exception)):
                    await _handle_upload(self.request(body), endpoint)
                self.assertEqual(len(files), 1)
                self.assertTrue(files[0].closed)

    async def test_parser_stream_exception_and_cancellation_close_incomplete_file(self):
        body = b'--small-test\r\nContent-Disposition: form-data; name="file"; filename="ref.png"\r\n\r\nsmall'
        for error in (RuntimeError('controlled receive failure'), asyncio.CancelledError()):
            async def endpoint(request):
                self.fail('partial parser must not enter endpoint')
            with tracked_spools(force_disk=True) as files:
                with self.assertRaises(type(error)):
                    await _handle_upload(self.request(body, receive_error=error), endpoint)
                self.assertEqual(len(files), 1)
                self.assertTrue(files[0].closed)

    async def test_anyio_cancellation_scope_cannot_skip_form_close(self):
        body = multipart([('file', 'reference.png', png()), ('client_project_id', None, b'story-one')])
        with tracked_spools(force_disk=True) as files:
            with anyio.CancelScope() as cancellation:
                async def endpoint(request):
                    cancellation.cancel()
                    await anyio.sleep(0)
                await _handle_upload(self.request(body), endpoint)
            self.assertEqual(len(files), 1)
            self.assertTrue(files[0].closed)

    async def test_nonmultipart_rejected_without_reading_request_body(self):
        calls = 0
        async def receive():
            nonlocal calls
            calls += 1
            self.fail('unsupported content type must not read its body')
        async def endpoint(request):
            self.fail('unsupported content type must not enter endpoint')
        for content_type in (None, 'application/json', 'application/x-www-form-urlencoded'):
            request = Request({'type': 'http', 'method': 'POST', 'path': '/v1/assets',
                'headers': [] if content_type is None else [(b'content-type', content_type.encode())],
                'app': object()}, receive)
            with self.assertRaises(HTTPException) as caught:
                await _handle_upload(request, endpoint)
            self.assertEqual(caught.exception.status_code, 415)
        self.assertEqual(calls, 0)


class UploadRouteThreadCancellationTests(unittest.IsolatedAsyncioTestCase):
    async def exercise_thread_cancellation(self, mode, *, handler_error=False):
        """Real File/Form dispatch into AnyIO's thread pool, not an async stub."""
        app = FastAPI()
        started, release, finished = (threading.Event() for _ in range(3))
        observations = {}

        def endpoint(file: UploadFile = File(...), client_project_id: str = Form(...)):
            observations['project'] = client_project_id
            started.set()
            try:
                # A small controlled stand-in for an already running bounded
                # decoder. The event is always released by the test finally.
                if not release.wait(5):
                    raise RuntimeError('test worker was not released')
                observations['closed_in_worker'] = file.file.closed
                observations['read'] = file.file.read(1)
                if handler_error:
                    raise RuntimeError('controlled handler failure')
                return {'ok': True}
            finally:
                finished.set()

        app.router.add_api_route('/v1/assets', endpoint, methods=['POST'],
                                 route_class_override=AssetUploadRoute)
        body = multipart([('file', 'reference.png', png()),
                          ('client_project_id', None, b'story-one')])
        received = False
        scopes, messages = [], []

        async def receive():
            nonlocal received
            if not received:
                received = True
                return {'type': 'http.request', 'body': body, 'more_body': False}
            await asyncio.sleep(10)

        async def send(message):
            messages.append(message)

        scope = {'type': 'http', 'asgi': {'version': '3.0'}, 'http_version': '1.1',
                 'method': 'POST', 'path': '/v1/assets', 'raw_path': b'/v1/assets',
                 'root_path': '', 'scheme': 'http', 'query_string': b'',
                 'headers': [(b'content-type', b'multipart/form-data; boundary=small-test')],
                 'server': ('testserver', 80), 'client': ('test', 1)}

        async def request():
            if mode == 'scope':
                with anyio.CancelScope() as cancellation:
                    scopes.append(cancellation)
                    await app(scope, receive, send)
                    # Ensure deferred scope cancellation is observed even if
                    # the in-memory ASGI send did not itself checkpoint.
                    await anyio.lowlevel.checkpoint()
                observations['scope_cancelled'] = cancellation.cancelled_caught
            else:
                await app(scope, receive, send)

        async def wait_for_event(event):
            for _ in range(1000):
                if event.is_set():
                    return
                await asyncio.sleep(.001)
            self.fail('bounded test worker did not reach expected point')

        with tracked_spools(force_disk=True) as files:
            task = asyncio.create_task(request())
            try:
                await wait_for_event(started)
                self.assertEqual(len(files), 1)
                if mode == 'scope':
                    scopes[0].cancel()
                else:
                    task.cancel()
                for _ in range(3 if mode == 'repeated' else 1):
                    await asyncio.sleep(.02)
                    self.assertFalse(task.done(), 'request/admission must await the actual worker')
                    self.assertFalse(files[0].closed, 'live worker must retain its upload file')
                    self.assertFalse(finished.is_set())
                    if mode == 'repeated':
                        task.cancel()
            finally:
                release.set()
                # Always join before test cleanup can close the tracked spool.
                try:
                    await asyncio.wait_for(asyncio.shield(task), 3)
                except asyncio.CancelledError:
                    observations['native_cancelled'] = True
                await wait_for_event(finished)
            self.assertFalse(observations['closed_in_worker'])
            self.assertEqual(observations['read'], png()[:1])
            self.assertEqual(observations['project'], 'story-one')
            self.assertTrue(files[0].closed)
            if mode == 'scope':
                self.assertTrue(observations['scope_cancelled'])
            else:
                self.assertTrue(observations['native_cancelled'])
                self.assertFalse(any(message['type'] == 'http.response.start' for message in messages))

    async def test_anyio_scope_waits_for_real_sync_endpoint(self):
        await self.exercise_thread_cancellation('scope')

    async def test_native_task_cancel_waits_for_real_sync_endpoint(self):
        await self.exercise_thread_cancellation('native')

    async def test_repeated_native_cancel_cannot_abandon_real_sync_endpoint(self):
        await self.exercise_thread_cancellation('repeated')

    async def test_cancelled_caller_still_joins_and_retrieves_handler_error(self):
        await self.exercise_thread_cancellation('native', handler_error=True)


class UploadRouteParserThreadTests(unittest.IsolatedAsyncioTestCase):
    async def exercise_disk_operation(self, operation, mode):
        started, release, finished = (threading.Event() for _ in range(3))
        opened, scopes, observations = [], [], {}
        real_spool = formparsers.SpooledTemporaryFile
        event_loop_thread = threading.get_ident()

        def create(*args, **kwargs):
            file = real_spool(*args, **kwargs)
            file.rollover()  # Real disk file even for this tiny PNG.
            original = getattr(file, operation)

            def blocked(*args):
                observations['worker_thread'] = threading.get_ident()
                started.set()
                try:
                    if not release.wait(5):
                        raise RuntimeError('test disk operation was not released')
                    observations['closed_in_worker'] = file.closed
                    result = original(*args)
                    observations['operation_completed'] = True
                    return result
                finally:
                    finished.set()

            setattr(file, operation, blocked)
            opened.append(file)
            return file

        async def endpoint(request):
            observations['entered_endpoint'] = True

        body = multipart([('file', 'reference.png', png()),
                          ('client_project_id', None, b'story-one')])
        request = UploadRouteCleanupTests().request(body)

        async def run():
            if mode == 'scope':
                with anyio.CancelScope() as cancellation:
                    scopes.append(cancellation)
                    await _handle_upload(request, endpoint)
                    await anyio.lowlevel.checkpoint()
                observations['scope_cancelled'] = cancellation.cancelled_caught
            else:
                await _handle_upload(request, endpoint)

        with mock.patch.object(formparsers, 'SpooledTemporaryFile', side_effect=create):
            task = asyncio.create_task(run())
            try:
                for _ in range(1000):
                    if started.is_set():
                        break
                    await asyncio.sleep(.001)
                self.assertTrue(started.is_set())
                self.assertEqual(len(opened), 1)
                if mode == 'scope':
                    scopes[0].cancel()
                else:
                    task.cancel()
                for _ in range(3 if mode == 'repeated' else 1):
                    await asyncio.sleep(.02)
                    self.assertFalse(task.done(), 'do not release admission before disk I/O finishes')
                    self.assertFalse(opened[0].closed)
                    self.assertFalse(finished.is_set())
                    if mode == 'repeated':
                        task.cancel()
            finally:
                release.set()
                try:
                    await asyncio.wait_for(asyncio.shield(task), 3)
                except asyncio.CancelledError:
                    observations['native_cancelled'] = True
                finally:
                    observations['closed_before_test_cleanup'] = all(file.closed for file in opened)
                    for file in opened:
                        file.close()
            self.assertTrue(finished.is_set())
            self.assertNotEqual(observations['worker_thread'], event_loop_thread)
            self.assertFalse(observations['closed_in_worker'])
            self.assertTrue(observations['operation_completed'])
            self.assertNotIn('entered_endpoint', observations)
            self.assertTrue(observations['closed_before_test_cleanup'])
            self.assertTrue(all(file.closed for file in opened))
            self.assertTrue(observations['scope_cancelled' if mode == 'scope' else 'native_cancelled'])

    async def test_real_disk_write_finishes_before_scope_or_repeated_native_cancellation(self):
        for mode in ('scope', 'native', 'repeated'):
            with self.subTest(mode=mode):
                await self.exercise_disk_operation('write', mode)

    async def test_real_disk_seek_finishes_before_scope_or_repeated_native_cancellation(self):
        for mode in ('scope', 'native', 'repeated'):
            with self.subTest(mode=mode):
                await self.exercise_disk_operation('seek', mode)

    async def test_waiting_for_more_upload_bytes_remains_cancellable(self):
        waiting = asyncio.Event()
        received = False
        body = b'--small-test\r\nContent-Disposition: form-data; name="file"; filename="ref.png"\r\n\r\nsmall'

        async def receive():
            nonlocal received
            if not received:
                received = True
                return {'type': 'http.request', 'body': body, 'more_body': True}
            waiting.set()
            await asyncio.Event().wait()

        async def endpoint(request):
            self.fail('cancelled partial upload must not enter endpoint')

        request = Request({'type': 'http', 'method': 'POST', 'path': '/v1/assets',
            'headers': [(b'content-type', b'multipart/form-data; boundary=small-test')],
            'app': object()}, receive)
        with tracked_spools(force_disk=True) as files:
            task = asyncio.create_task(_handle_upload(request, endpoint))
            await asyncio.wait_for(waiting.wait(), 1)
            self.assertEqual(len(files), 1)
            self.assertFalse(files[0].closed)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 1)
            self.assertTrue(files[0].closed)


if __name__ == '__main__':
    unittest.main()
