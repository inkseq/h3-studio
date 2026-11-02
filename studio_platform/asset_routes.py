"""Asset reads, writes and the original scoped object-download transport."""

from __future__ import annotations
import re
from urllib.parse import quote
from fastapi import File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import RedirectResponse, StreamingResponse
from .http_limits import ADMISSION_SCOPE_KEY
from .media_renditions import public_renditions, RENDITIONS
from .upload_route import AssetUploadRoute
from .storage import (
    LocalObjectStore,
    StorageError,
    S3ObjectStore,
    VIDEO_SIGNING_BUCKET_SECONDS,
    VIDEO_SIGNING_VALIDITY_SECONDS,
)


def register_routes(app, *, services, clock, cache_scope_key):
    asset_service, storage = services.assets, services.storage
    authorized_project = services.access.project

    def upload_asset(
        request: Request,
        file: UploadFile = File(...),
        client_project_id: str = Form(...),
        client_asset_id: str | None = Form(None),
    ):
        principal = request.state.principal
        authorized_project(principal, client_project_id, "assets:write")
        return asset_service.upload(
            principal.owner,
            client_project_id,
            file.file,
            file.filename or "file",
            client_asset_id=client_asset_id,
        )

    app.router.add_api_route(
        "/v1/assets",
        upload_asset,
        methods=["POST"],
        status_code=201,
        route_class_override=AssetUploadRoute,
    )

    @app.get("/v1/assets")
    def list_assets(request: Request, client_project_id: str):
        principal = request.state.principal
        authorized_project(principal, client_project_id, "assets:read")
        return {"assets": asset_service.list(principal.owner, client_project_id)}

    @app.get("/v1/storage-usage")
    def storage_usage(request: Request):
        if request.state.principal.machine:
            raise HTTPException(403, "服务身份不能查询账户整体存储用量")
        return asset_service.usage(request.state.principal.owner)

    @app.get("/v1/assets/{asset_id}")
    def get_asset(asset_id: str, request: Request):
        principal = request.state.principal
        value = asset_service.get(principal.owner, asset_id)
        authorized_project(principal, value["project_id"], "assets:read")
        return asset_service.public(value)

    @app.post("/v1/assets/{asset_id}/resume")
    def resume_asset(asset_id: str, request: Request):
        principal = request.state.principal
        value = asset_service.get(principal.owner, asset_id)
        authorized_project(principal, value["project_id"], "assets:write")
        # Browser callers cannot override an active/stale worker lease. They
        # resume the same durable receipt, never create a fresh storage key.
        return asset_service.resume(principal.owner, asset_id)

    object_content = object_reader(
        storage, clock=clock, cache_scope_key=cache_scope_key
    )

    @app.api_route("/v1/assets/{asset_id}/content", methods=["GET", "HEAD"])
    def asset_content(
        asset_id: str,
        request: Request,
        download: bool = False,
        rendition: str | None = Query(None),
        expected_account: str | None = Query(
            None,
            description="Optional browser account fence for native media/navigation; does not grant access",
        ),
    ):
        principal = request.state.principal
        value = asset_service.get(principal.owner, asset_id)
        authorized_project(principal, value["project_id"], "assets:read")
        if value["status"] != "ready":
            raise HTTPException(409, "素材尚未就绪")
        if "rendition" in request.query_params:
            if (
                request.query_params.getlist("rendition") != [rendition]
                or rendition not in RENDITIONS
            ):
                raise HTTPException(400, "invalid_rendition")
            advertised = public_renditions(value).get("renditions", {}).get(rendition)
            if advertised is None:
                raise HTTPException(404, "rendition_not_ready")
            item = value[rendition]
            return object_content(
                item["key"],
                advertised["content_type"],
                f'{asset_id}-{rendition}.webp',
                request,
                download=download,
                immutable_rendition=True,
            )
        original = value["original"]
        return object_content(
            original["key"],
            value["mime"],
            value["file_name"],
            request,
            download=download,
            # Ready uploads are hash-verified and never rewritten in place.
            immutable_media=isinstance(original.get("sha256"), str)
            and re.fullmatch(r"[0-9a-f]{64}", original["sha256"]) is not None
            and isinstance(value["mime"], str)
            and value["mime"].split("/")[0] in {"image", "video", "audio"},
        )

    @app.post("/v1/assets/{asset_id}/derivatives", status_code=201)
    def derivative(asset_id: str, request: Request, body: dict):
        principal = request.state.principal
        parent = asset_service.get(principal.owner, asset_id)
        authorized_project(principal, parent["project_id"], "assets:write")
        if set(body) - {"start", "end"}:
            raise HTTPException(422, "仅支持明确的时间选段")
        return asset_service.derive(
            principal.owner, asset_id, body.get("start"), body.get("end")
        )

    return object_content


def object_reader(storage, *, clock, cache_scope_key):
    """Bind downloads to the original storage and request admission lease."""

    def object_content(
        key,
        mime,
        filename,
        request,
        *,
        download=False,
        immutable_video=False,
        immutable_rendition=False,
        immutable_media=False,
    ):
        headers = {
            "Accept-Ranges": "bytes",
            "Content-Disposition": ("attachment" if download else "inline")
            + "; filename*=UTF-8''"
            + quote(filename, safe=""),
        }
        if not isinstance(storage, LocalObjectStore):
            if request.method == "HEAD":
                # A GET presign cannot authenticate a redirected HEAD request.
                info = storage.stat(key)
                headers["Content-Length"] = str(info.size_bytes)
                return StreamingResponse(iter(()), media_type=mime, headers=headers)
            options = {"download_filename": filename} if download else {}
            url = None
            if (
                (immutable_video or immutable_rendition or immutable_media)
                and isinstance(storage, S3ObjectStore)
                and storage.config.provider == "r2"
            ):
                signer = (
                    storage.presign_immutable_rendition
                    if immutable_rendition
                    else storage.presign_immutable_media
                    if immutable_media
                    else storage.presign_immutable_video
                )
                try:
                    url = signer(key, content_type=mime, **options)
                except StorageError:
                    if not immutable_media:
                        raise
                    # Without the bucketed signer an upload keeps its short presign.
            if url is not None:
                now = clock()
                # Use the issued URL's bucket, including a rollover during signing.
                bucket_end = (
                    url.expires_at
                    - VIDEO_SIGNING_VALIDITY_SECONDS
                    + VIDEO_SIGNING_BUCKET_SECONDS
                )
                if immutable_video or immutable_media:
                    request.scope[cache_scope_key] = max(
                        0, min(int(url.expires_at - now) - 600, int(bucket_end - now))
                    )
            else:
                url = storage.presign_download(key, expires_seconds=300, **options)
            return RedirectResponse(
                url.reveal(),
                status_code=307,
                headers={
                    "Cache-Control": "private, no-store",
                    "Referrer-Policy": "no-referrer",
                },
            )
        info = storage.stat(key)
        start, end, status = 0, info.size_bytes - 1, 200
        requested_range = request.headers.get("range")
        if requested_range:
            match = re.fullmatch(r"bytes=([0-9]{0,20})-([0-9]{0,20})", requested_range)
            if not match or not any(match.groups()):
                raise HTTPException(
                    416,
                    "无效字节范围",
                    headers={"Content-Range": f"bytes */{info.size_bytes}"},
                )
            left, right = match.groups()
            if left:
                start = int(left)
                end = min(int(right) if right else end, end)
            else:
                start = max(0, info.size_bytes - int(right))
            if start > end or start >= info.size_bytes:
                raise HTTPException(
                    416,
                    "字节范围越界",
                    headers={"Content-Range": f"bytes */{info.size_bytes}"},
                )
            status = 206
            headers["Content-Range"] = f"bytes {start}-{end}/{info.size_bytes}"
        headers["Content-Length"] = str(end - start + 1)

        def stream():
            with storage.open(key) as source:
                # Starlette's threaded iterator may remain suspended at yield
                # after a disconnected/expired response. The outer ASGI lease
                # deterministically closes the handle instead of relying on
                # generator garbage collection or a success-only background task.
                request.scope[ADMISSION_SCOPE_KEY].release_when_finished(source.close)
                source.seek(start)
                remaining = end - start + 1
                while remaining:
                    chunk = source.read(min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    yield chunk

        return StreamingResponse(
            iter(()) if request.method == "HEAD" else stream(),
            status_code=status,
            media_type=mime,
            headers=headers,
        )

    return object_content
