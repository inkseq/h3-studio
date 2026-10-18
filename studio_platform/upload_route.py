"""Strict form admission for the asset upload route, retaining File/Form schema.

Register only the asset upload POST with this APIRoute. The multipart parser
subclass and request._form cache integration are covered by compatibility tests
against pinned Starlette 1.7.0 / python-multipart 0.0.32. Upstream's finalize()
does not reject a truncated normal EOF, so completion and all temporary file
handles are checked here. Upstream header/boundary limits remain unchanged.
"""

from __future__ import annotations

import asyncio
import anyio
from fastapi import HTTPException, Request
from fastapi.routing import APIRoute
from python_multipart.multipart import parse_options_header
from starlette.datastructures import UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser


UPLOAD_FORM_LIMITS = {"max_files": 1, "max_fields": 2, "max_part_size": 4096}
_FIELDS = frozenset({"file", "client_project_id", "client_asset_id"})
_FIELD_ERROR = "素材上传仅允许一个file、一个client_project_id和至多一个client_asset_id，不接受未知或重复字段"


class _JoinedUploadFile(UploadFile):
    """Join only this parser's local disk operations, never the request stream."""

    async def write(self, data):
        return await _finish_before_cancelling(super().write(data))

    async def seek(self, offset):
        return await _finish_before_cancelling(super().seek(offset))


class StrictAssetParser(MultiPartParser):
    def __init__(self, headers, stream, *, allowed_fields=_FIELDS):
        super().__init__(headers, stream, **UPLOAD_FORM_LIMITS)
        self.allowed_fields = allowed_fields
        self._asset_seen_fields = set()
        self._asset_complete = False

    def on_headers_finished(self):
        super().on_headers_finished()
        name = self._current_part.field_name
        if name not in self.allowed_fields or name in self._asset_seen_fields:
            raise MultiPartException(_FIELD_ERROR)
        self._asset_seen_fields.add(name)
        if self._current_part.file is not None:
            # Pinned Starlette integration: the part has not entered FormData
            # yet. Keep the exact spool, metadata, size and upstream limits;
            # only its awaited write/seek behavior changes. A parser exception
            # must not close the spool while either disk thread still uses it.
            file = self._current_part.file
            self._current_part.file = _JoinedUploadFile(
                file.file, size=file.size, filename=file.filename, headers=file.headers
            )

    def on_end(self):
        super().on_end()
        self._asset_complete = True

    def close_files(self):
        # Includes an unfinished part which never entered returned FormData.
        # These are local SpooledTemporaryFile handles, with idempotent close.
        for file in self._files_to_close_on_error:
            file.close()

    async def parse(self):
        try:
            form = await super().parse()
            if not self._asset_complete:
                raise MultiPartException("素材上传表单未完整接收；请重新上传原文件")
            return form
        except BaseException:
            self.close_files()
            raise


async def _finish_before_cancelling(operation):
    """Keep the upload file alive until its synchronous consumer has finished.

    AnyIO's default thread shield handles CancelScope cancellation, but native
    asyncio Task.cancel() can still detach its awaiting task from a live thread.
    This asyncio/Uvicorn upload route therefore owns and joins the operation:
    cancellation is remembered, not forwarded to it. Repeated native cancellation
    also cannot release the enclosing request/admission or close its files early.
    This is not a thread kill or a graceful-shutdown time bound; cancellation may
    wait for the existing bounded media steps or disk I/O. It is deliberately
    never used for receiving the upload stream. Keep this route limited to the
    current File/Form endpoint, without task-affine yield dependencies.
    """
    interrupted = None
    with anyio.CancelScope(shield=True):
        task = asyncio.create_task(operation)
        while True:
            try:
                response = await asyncio.shield(task)
                break
            except asyncio.CancelledError as error:
                if task.cancelled():
                    # The operation itself finished by cancellation, rather
                    # than receiving the outer request's cancellation.
                    raise interrupted or error
                interrupted = interrupted or error
            except BaseException:
                # Awaiting shield retrieves the operation exception even after
                # the caller cancels; do not leave a background task behind.
                if interrupted is not None:
                    raise interrupted from None
                raise
        if interrupted is not None:
            raise interrupted
        return response


async def _handle_upload(request: Request, original_handler, *, allowed_fields=_FIELDS):
    parser = None
    try:
        content_type, _ = parse_options_header(request.headers.get("Content-Type"))
        if content_type != b"multipart/form-data":
            # This route has the larger file-upload body budget; never send
            # URL-encoded/JSON bodies into a different in-memory form parser.
            raise HTTPException(415, "素材上传须使用multipart/form-data")
        parser = StrictAssetParser(
            request.headers, request.stream(), allowed_fields=allowed_fields
        )
        try:
            form = await parser.parse()
        except MultiPartException as error:
            raise HTTPException(400, error.message) from None
        # Narrow, version-pinned cache integration: original File/Form
        # binding reads exactly this form; never parses the body again.
        request._form = form
        seen = set()
        for name, _ in form.multi_items():
            if name not in allowed_fields or name in seen:
                raise HTTPException(400, _FIELD_ERROR)
            seen.add(name)
        # Required fields and their types remain FastAPI's normal 422 contract.
        # The original handler reuses the public form cache, without reparsing.
        # Honor deferred scope cancellation after a completed disk operation
        # before entering any new endpoint work.
        await anyio.lowlevel.checkpoint()
        return await _finish_before_cancelling(original_handler(request))
    finally:
        try:
            # Join async disk-spooled close even after repeated native cancel.
            await _finish_before_cancelling(request.close())
        finally:
            if parser is not None:
                parser.close_files()


class AssetUploadRoute(APIRoute):
    def get_route_handler(self):
        original_handler = super().get_route_handler()

        async def strict_asset_upload(request: Request):
            return await _handle_upload(request, original_handler)

        return strict_asset_upload


class QuickChatAssetUploadRoute(APIRoute):
    """Session ID comes from the authenticated URL, not a multipart project ID."""

    def get_route_handler(self):
        original_handler = super().get_route_handler()

        async def strict_session_upload(request: Request):
            return await _handle_upload(
                request,
                original_handler,
                allowed_fields=frozenset({"file", "client_asset_id"}),
            )

        return strict_session_upload
