"""Read-only web releases, selected per HTML request without restarting the API.

The host publisher owns immutable release directories and append-only assets.
Only an atomic replacement of current.json changes the active HTML. This module
never writes to that mount or serves its pointer/release metadata as static files.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import stat

from fastapi import HTTPException, Request
from fastapi.responses import FileResponse


FRONTEND_CONTRACT = "sixnine-web-v1"
HTML_ROUTES = frozenset(
    {
        "/",
        "/index.html",
        "/app",
        "/app/",
        "/freestyle",
        "/freestyle/",
        "/quick-chat",
        "/quick-chat/",
        "/operator",
        "/operator/",
        "/settings",
        "/settings/",
        "/library",
        "/library/",
        "/trash",
        "/trash/",
        "/pricing",
        "/pricing/",
        "/terms",
        "/terms/",
        "/privacy",
        "/privacy/",
        "/content",
        "/content/",
    }
)
STATIC_CACHE_SCOPE_KEY = "sixnine.frontend_cache"
_COMMIT = re.compile(r"[0-9a-f]{40}\Z")
_HASHED_ASSET = re.compile(r".+-[A-Za-z0-9_-]{8,}\.[A-Za-z0-9]+\Z")
_VIDEO_CACHE_WORKER = re.compile(r"video-cache-worker-[A-Za-z0-9_-]{8,}\.js\Z")
_MAX_POINTER_BYTES = 1024


def _linked(path: Path, metadata: os.stat_result) -> bool:
    # Windows junctions are reparse points, not necessarily POSIX symlinks.
    return stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def validate_release_directory(value: Path) -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise ValueError("Frontend release directory must be an absolute path")
    try:
        metadata = path.lstat()
        if _linked(path, metadata) or not stat.S_ISDIR(metadata.st_mode):
            raise ValueError("Frontend release directory must be a real directory")
        return path.resolve(strict=True)
    except OSError:
        raise ValueError(
            "Frontend release directory must exist and be accessible"
        ) from None


def is_public_frontend(settings, method: str, path: str) -> bool:
    return bool(
        (settings.frontend_dir is not None or settings.frontend_release_dir is not None)
        and method in {"GET", "HEAD"}
        and (path in HTML_ROUTES or path.startswith("/assets/"))
    )


def _regular_file(root: Path, *parts: str):
    """Reject links and special files at every component below the fixed root."""
    current = root
    metadata = current.lstat()
    if _linked(current, metadata) or not stat.S_ISDIR(metadata.st_mode):
        raise ValueError("Invalid static directory")
    for index, part in enumerate(parts):
        current /= part
        metadata = current.lstat()
        if _linked(current, metadata):
            raise ValueError("Static links are not served")
        expected = stat.S_ISREG if index == len(parts) - 1 else stat.S_ISDIR
        if not expected(metadata.st_mode):
            raise ValueError("Invalid static file")
    return current, metadata


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate frontend pointer field")
        result[key] = value
    return result


def select_html(settings):
    """Return one immutable path/stat/commit, reading the pointer exactly once."""
    root = settings.frontend_release_dir
    if root is not None:
        try:
            pointer_path, _ = _regular_file(root, "current.json")
        except FileNotFoundError:
            # A missing mount is a fault, not an absent pointer during bootstrap.
            validate_release_directory(root)
        else:
            with pointer_path.open("rb") as source:
                if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                    raise ValueError("Invalid frontend pointer")
                raw = source.read(_MAX_POINTER_BYTES + 1)
            if len(raw) > _MAX_POINTER_BYTES:
                raise ValueError("Invalid frontend pointer size")
            pointer = json.loads(raw, object_pairs_hook=_unique_object)
            if (
                not isinstance(pointer, dict)
                or set(pointer) != {"version", "commit", "api_contract"}
                or type(pointer["version"]) is not int
                or pointer["version"] != 1
                or pointer["api_contract"] != FRONTEND_CONTRACT
                or not isinstance(pointer["commit"], str)
                or not _COMMIT.fullmatch(pointer["commit"])
            ):
                raise ValueError("Unsupported frontend pointer")
            path, metadata = _regular_file(
                root, "releases", pointer["commit"], "index.html"
            )
            return path, metadata, pointer["commit"]
    if settings.frontend_dir is None:
        raise FileNotFoundError("No frontend release is available")
    path, metadata = _regular_file(settings.frontend_dir, "index.html")
    return path, metadata, None


def _asset_parts(path: str):
    parts = path.split("/")
    if (
        not path
        or any(not p or p.startswith(".") for p in parts)
        or any(c in path for c in ("\\", "\x00", ":"))
    ):
        raise HTTPException(404, "Not Found")
    return parts


def register_routes(app):
    settings = app.state.settings
    if settings.frontend_dir is None and settings.frontend_release_dir is None:
        return
    if settings.frontend_dir is not None:
        try:
            _regular_file(settings.frontend_dir, "index.html")
        except (OSError, ValueError):
            raise ValueError(
                "Configured frontend build is missing or invalid; build the reviewed Inkseq source snapshot first"
            ) from None

    def html(request: Request):
        try:
            path, metadata, commit = select_html(settings)
        except (OSError, ValueError, UnicodeError):
            raise HTTPException(
                503, "网站版本暂时不可用，请稍后重试", headers={"Retry-After": "5"}
            ) from None
        headers = {
            "Cache-Control": "no-cache",
            "X-Sixnine-Frontend-Contract": FRONTEND_CONTRACT,
        }
        if commit is not None:
            headers["X-Sixnine-Frontend-Commit"] = commit
        request.scope[STATIC_CACHE_SCOPE_KEY] = headers["Cache-Control"]
        return FileResponse(
            path, media_type="text/html", stat_result=metadata, headers=headers
        )

    for route in sorted(HTML_ROUTES):
        app.add_api_route(route, html, methods=["GET", "HEAD"], include_in_schema=False)

    @app.api_route(
        "/assets/{asset_path:path}", methods=["GET", "HEAD"], include_in_schema=False
    )
    def asset(asset_path: str, request: Request):
        parts = _asset_parts(asset_path)
        roots = [
            root
            for root in (settings.frontend_release_dir, settings.frontend_dir)
            if root is not None
        ]
        for root in roots:
            try:
                path, metadata = _regular_file(root, "assets", *parts)
            except FileNotFoundError:
                continue
            except (OSError, ValueError):
                # A link/invalid external path must not be hidden by a bundle copy.
                raise HTTPException(404, "Not Found") from None
            cache = (
                "public, max-age=31536000, immutable"
                if _HASHED_ASSET.fullmatch(parts[-1])
                else "no-cache"
            )
            headers = {"Cache-Control": cache}
            # Only the reviewed hashed video cache worker can claim the root.
            # Other JS/assets keep their default directory-limited scope.
            if len(parts) == 1 and _VIDEO_CACHE_WORKER.fullmatch(parts[0]):
                headers["Service-Worker-Allowed"] = "/"
            request.scope[STATIC_CACHE_SCOPE_KEY] = cache
            return FileResponse(path, stat_result=metadata, headers=headers)
        raise HTTPException(404, "Not Found")
