"""Streaming body limit; Content-Length is only an early rejection hint."""

import asyncio
import time
from collections import Counter
import threading
import re
from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse, RedirectResponse


ADMISSION_SCOPE_KEY = "sixnine.request_admission"
AGENT_CONNECT_BODY_BYTES = 8192


def body_limit(path, project_bytes, upload_bytes):
    if path in {
        "/v1/agent-connect/exchange",
        "/v1/agent-connect/grant-preview",
    } or path.startswith("/v1/account/agent-connections"):
        return min(project_bytes, AGENT_CONNECT_BODY_BYTES)
    return upload_bytes + 1024 * 1024 if is_asset_upload(path) else project_bytes


def is_asset_upload(path):
    return path == "/v1/assets" or bool(
        re.fullmatch(r"/v1/quick-chat/sessions/[^/]+/assets", path)
    )


class RequestAdmission:
    """Fail-fast process-local bounds; leases last until ASGI response completion.

    Authentication has a short separate gate so waiting for the database cannot
    consume the whole sync thread pool. Machine clients share their owner's
    allowance. A reverse proxy/shared limiter is still needed across processes.
    """

    def __init__(
        self,
        *,
        total=256,
        authentication=32,
        login=2,
        per_owner=64,
        downloads=32,
        downloads_per_owner=16,
        thumbnails=64,
        thumbnails_per_owner=32,
    ):
        values = (
            total,
            authentication,
            login,
            per_owner,
            downloads,
            downloads_per_owner,
            thumbnails,
            thumbnails_per_owner,
        )
        if any(type(x) is not int or x < 1 for x in values):
            raise ValueError("Invalid request admission limit")
        self.limits = {
            "total": total,
            "authentication": authentication,
            "login": login,
            "owner": per_owner,
            "downloads": downloads,
            "owner_downloads": downloads_per_owner,
            "thumbnails": thumbnails,
            "owner_thumbnails": thumbnails_per_owner,
        }
        self.counts = Counter()
        self.lock = threading.Lock()

    def acquire(self, kind, owner=None):
        key = (kind, owner)
        with self.lock:
            if self.counts[key] >= self.limits[kind]:
                return False
            self.counts[key] += 1
            return True

    def release(self, kind, owner=None):
        key = (kind, owner)
        with self.lock:
            self.counts[key] -= 1
            if self.counts[key] <= 0:
                del self.counts[key]


class RequestLease:
    def __init__(self, admission):
        self.admission, self.held, self.callbacks = admission, [], []

    def acquire(self, kind, owner=None):
        if not self.admission.acquire(kind, owner):
            return False
        self.held.append((kind, owner))
        return True

    def release(self, kind, owner=None):
        key = (kind, owner)
        if key in self.held:
            self.held.remove(key)
            self.admission.release(kind, owner)

    def release_when_finished(self, callback):
        self.callbacks.append(callback)

    def close(self):
        try:
            for callback in self.callbacks:
                callback()
        finally:
            self.callbacks.clear()
            for kind, owner in self.held:
                self.admission.release(kind, owner)
            self.held.clear()


def hosted_error_redirect(scope, code, *, retry_after=None):
    """A fixed landing page for failed hosted browser navigation only."""
    if (
        not scope
        or scope.get("method") != "GET"
        or scope.get("path")
        not in {
            "/api/auth/workos/start",
            "/api/auth/workos/callback",
        }
    ):
        return None
    headers = {key.lower(): value.lower() for key, value in scope.get("headers", [])}
    if headers.get(
        b"sec-fetch-mode"
    ) != b"navigate" and b"text/html" not in headers.get(b"accept", b""):
        return None
    if code not in {
        "rate_limited",
        "state_invalid",
        "provider_unavailable",
        "account_deleted",
        "account_unavailable",
        "cancelled",
    }:
        raise ValueError("invalid_hosted_error_code")
    response_headers = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}
    if retry_after is not None:
        response_headers["Retry-After"] = str(retry_after)
    return RedirectResponse(
        "/?signin_error=" + code, status_code=302, headers=response_headers
    )


def admission_rejected(scope=None):
    redirect = hosted_error_redirect(scope, "rate_limited", retry_after=3)
    if redirect is not None:
        return redirect
    return JSONResponse(
        {
            "detail": "同时处理的请求已达到上限，请稍后重试",
            "code": "request_capacity_busy",
        },
        status_code=429,
        headers={
            "Retry-After": "3",
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


class RequestAdmissionMiddleware:
    def __init__(self, app, *, admission, send_idle_seconds=30, download_seconds=300):
        self.app, self.admission = app, admission
        self.send_idle_seconds, self.download_seconds = (
            send_idle_seconds,
            download_seconds,
        )

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        lease = RequestLease(self.admission)
        scope[ADMISSION_SCOPE_KEY] = lease
        started = False
        expected_bytes, emitted_bytes = None, 0
        path = scope.get("path", "")
        download = (
            scope.get("method") in {"GET", "HEAD"}
            and path.endswith("/content")
            and path.startswith(("/v1/assets/", "/v1/artifacts/"))
        )

        async def bounded_send(message):
            nonlocal started, expected_bytes, emitted_bytes
            if message["type"] == "http.response.start":
                if (
                    download
                    and scope.get("method") == "GET"
                    and message["status"] in {200, 206}
                ):
                    lengths = [
                        value
                        for key, value in message.get("headers", [])
                        if key.lower() == b"content-length"
                    ]
                    if (
                        len(lengths) == 1
                        and lengths[0].isdigit()
                        and len(lengths[0]) <= 20
                    ):
                        expected_bytes = int(lengths[0])
                started = True
            elif message["type"] == "http.response.body" and expected_bytes is not None:
                next_bytes = emitted_bytes + len(message.get("body", b""))
                if next_bytes > expected_bytes or (
                    not message.get("more_body", False) and next_bytes != expected_bytes
                ):
                    # BaseHTTPMiddleware can signal an empty end-of-stream
                    # before rethrowing a producer's read error. Never forward
                    # that false completion or depend on Uvicorn to catch it.
                    raise ResponseTimeout("incomplete_media_response")
                emitted_bytes = next_bytes
            try:
                await asyncio.wait_for(send(message), timeout=self.send_idle_seconds)
            except TimeoutError:
                raise ResponseTimeout("response_write_timeout") from None

        try:
            accepted = lease.acquire("total")
            if accepted and scope.get("path") == "/api/auth/login":
                accepted = lease.acquire("login")
            if not accepted:
                return await admission_rejected(scope)(scope, receive, bounded_send)
            if download:
                deadline = asyncio.timeout(self.download_seconds)
                try:
                    async with deadline:
                        await self.app(scope, receive, bounded_send)
                except TimeoutError:
                    if not deadline.expired():
                        raise  # Do not reinterpret an unrelated application error.
                    raise ResponseTimeout("media_response_deadline") from None
            else:
                await self.app(scope, receive, bounded_send)
        except ResponseTimeout:
            if not started:
                return await JSONResponse(
                    {"detail": "下载准备超时，请稍后重新读取原文件"},
                    status_code=504,
                    headers={"Cache-Control": "no-store", "Retry-After": "3"},
                )(scope, receive, bounded_send)
            # An incomplete response must close the connection, not append JSON
            # or pretend the remaining bytes arrived. Local media supports Range
            # for a later authenticated request; generation is never repeated.
            raise
        finally:
            # BaseHTTPMiddleware.call_next returns before a streamed response
            # has finished. The outer ASGI lifetime also covers slow clients,
            # disconnects, cancellations and response-generator exceptions.
            lease.close()


class ResponseTimeout(RuntimeError):
    """Static connection-abort reason; contains no URL, cookie or user content."""


class UploadAdmission:
    """Process-local ingress bounds for the reviewed single-Uvicorn deployment.

    This is before multipart spooling, independent of durable asset/CPU quotas.
    Multi-process deployments must also enforce a shared ingress limit.
    """

    def __init__(self, *, per_owner=2, total=4):
        self.per_owner, self.total = per_owner, total
        self.owners = Counter()
        self.lock = threading.Lock()

    def acquire(self, owner):
        with self.lock:
            if (
                self.owners[owner] >= self.per_owner
                or sum(self.owners.values()) >= self.total
            ):
                return False
            self.owners[owner] += 1
            return True

    def release(self, owner):
        with self.lock:
            self.owners[owner] -= 1
            if self.owners[owner] <= 0:
                del self.owners[owner]


class BodyLimitMiddleware:
    def __init__(
        self,
        app,
        *,
        project_bytes,
        upload_bytes,
        idle_seconds=30,
        total_seconds=900,
        login_seconds=15,
    ):
        self.app, self.project_bytes, self.upload_bytes = (
            app,
            project_bytes,
            upload_bytes,
        )
        self.idle_seconds, self.total_seconds = idle_seconds, total_seconds
        self.login_seconds = login_seconds

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = scope.get("path", "")
        maximum = body_limit(path, self.project_bytes, self.upload_bytes)
        if path == "/api/auth/login":
            maximum = min(maximum, 16 * 1024)
        total_seconds = (
            min(self.total_seconds, self.login_seconds)
            if path == "/api/auth/login"
            else self.total_seconds
        )
        consumed = 0
        started = time.monotonic()
        complete = False

        async def bounded_receive():
            nonlocal consumed, complete
            if complete:
                # A streaming response may keep listening for disconnect long
                # after the request body has ended; that is not a slow upload.
                return await receive()
            remaining = total_seconds - (time.monotonic() - started)
            if remaining <= 0:
                raise HTTPException(408, "上传接收超时，请检查网络后重试")
            try:
                message = await asyncio.wait_for(
                    receive(), timeout=min(self.idle_seconds, remaining)
                )
            except TimeoutError:
                raise HTTPException(408, "上传接收超时，请检查网络后重试") from None
            if message["type"] == "http.request":
                consumed += len(message.get("body", b""))
                if consumed > maximum:
                    # Raised before the overflowing chunk reaches JSON/multipart
                    # parsing. Starlette closes partial multipart temporary files.
                    raise HTTPException(413, "请求体超过限制")
                complete = not message.get("more_body", False)
            return message

        await self.app(scope, bounded_receive, send)
