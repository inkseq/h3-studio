"""Private, bounded object storage with explicit provider selection.

No cloud client, credential load, bucket creation or network request occurs on
import. Business services must authorize an owner before using a persisted key.
Use write_new for assets and persist its returned key, never a signed URL.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import stat as stat_module
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO, Protocol, runtime_checkable
from urllib.parse import quote, urlsplit

from .storage_config import (
    LOCAL_CAPABILITIES,
    S3Credentials,
    S3StorageConfig,
    StorageCapabilities,
    StorageConfigurationError,
)

DEFAULT_MAX_BYTES = 100 * 1024 * 1024
VIDEO_SIGNING_BUCKET_SECONDS = 12 * 3600
VIDEO_SIGNING_VALIDITY_SECONDS = 24 * 3600
VIDEO_OBJECT_CACHE_CONTROL = "private, max-age=86400, immutable"
_CHUNK = 1024 * 1024
_PART = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_RESERVED = re.compile(r"(?:CON|PRN|AUX|NUL|COM[0-9]|LPT[0-9])(?:\.|$)", re.I)
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_MIME = re.compile(r"[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+\Z")


class StorageError(Exception):
    """Safe-to-present storage error, with no credential or URL details."""


class InvalidObjectKey(StorageError, ValueError):
    pass


class ObjectNotFound(StorageError, FileNotFoundError):
    pass


class ObjectAlreadyExists(StorageError, FileExistsError):
    pass


class ObjectTooLarge(StorageError, ValueError):
    pass


class IntegrityError(StorageError):
    pass


class UnsupportedStorageOperation(StorageError):
    pass


class StorageWriteUncertain(StorageError):
    """Persist this key for HEAD/hash reconciliation; do not blindly write_new again."""

    def __init__(self, key: str):
        self.key = validate_key(key)
        super().__init__(
            "Object submission outcome is unknown; reconcile the object key before retrying"
        )


def _part(value: str) -> str:
    if (
        not isinstance(value, str)
        or not _PART.fullmatch(value)
        or value.endswith(".")
        or _RESERVED.match(value)
    ):
        raise InvalidObjectKey("Invalid object key component")
    return value


def validate_key(key: str) -> str:
    if not isinstance(key, str) or len(key) > 512 or not key or len(key.split("/")) > 8:
        raise InvalidObjectKey("Invalid object key")
    for value in key.split("/"):
        _part(value)
    return key


def make_object_key(owner_id: str, asset_id: str, filename: str = "blob") -> str:
    """Generate an opaque new physical key; original Unicode names belong in the DB."""
    for value in (owner_id, asset_id, filename):
        _part(value)
    if len(filename) > 64:
        raise InvalidObjectKey("Storage filename must be at most 64 ASCII characters")
    return validate_key(
        f"owners/{owner_id}/assets/{asset_id}/{uuid.uuid4().hex}-{filename}"
    )


def key_belongs_to(key: str, owner_id: str) -> bool:
    validate_key(key)
    _part(owner_id)
    return key.startswith(f"owners/{owner_id}/assets/")


def _content_type(value: str) -> str:
    if not isinstance(value, str) or len(value) > 128 or not _MIME.fullmatch(value):
        raise StorageError("Invalid media type")
    return value


def _limits(max_bytes: int, expected_sha256: str | None) -> None:
    if type(max_bytes) is not int or max_bytes < 0:
        raise StorageError("max_bytes must be a nonnegative integer")
    if expected_sha256 is not None and (
        not isinstance(expected_sha256, str) or not _HASH.fullmatch(expected_sha256)
    ):
        raise IntegrityError("Expected SHA256 must be a lowercase hexadecimal digest")


def _copy(
    source: BinaryIO, destination: BinaryIO, max_bytes: int, expected_sha256: str | None
) -> tuple[int, str]:
    _limits(max_bytes, expected_sha256)
    digest = hashlib.sha256()
    count = 0
    while True:
        chunk = source.read(min(_CHUNK, max_bytes - count + 1))
        if not isinstance(chunk, bytes):
            raise StorageError("Upload source must be a binary stream")
        if not chunk:
            break
        count += len(chunk)
        if count > max_bytes:
            raise ObjectTooLarge("Object exceeds the configured upload limit")
        digest.update(chunk)
        destination.write(chunk)
    checksum = digest.hexdigest()
    if expected_sha256 is not None and checksum != expected_sha256:
        raise IntegrityError("Object checksum does not match")
    return count, checksum


@dataclass(frozen=True)
class ObjectInfo:
    provider: str
    key: str
    size_bytes: int
    sha256: str | None
    content_type: str
    etag: str | None = None
    version_id: str | None = None


class SecretURL:
    """A bearer capability. Only reveal() is suitable for an authorized HTTP response.

    Not a dataclass: asdict/jsonable_encoder must not accidentally serialize it.
    The caller must also disable response-body/query logging and analytics capture.
    """

    __slots__ = ("_url", "method", "expires_at", "_headers")

    def __init__(
        self, url: str, method: str, expires_at: float, headers: dict[str, str]
    ):
        self._url, self.method, self.expires_at, self._headers = (
            url,
            method,
            expires_at,
            dict(headers),
        )

    def __repr__(self):
        return f"SecretURL(method={self.method!r}, url=<redacted>)"

    __str__ = __repr__

    def __reduce_ex__(self, protocol):
        raise TypeError("Signed object access must not be serialized")

    def reveal(self) -> str:
        return self._url

    @property
    def required_headers(self) -> dict[str, str]:
        return dict(self._headers)


@runtime_checkable
class ObjectStore(Protocol):
    capabilities: StorageCapabilities

    def write_new(
        self,
        owner_id: str,
        asset_id: str,
        source: BinaryIO,
        *,
        filename: str = "blob",
        content_type: str = "application/octet-stream",
        max_bytes: int = DEFAULT_MAX_BYTES,
        expected_sha256: str | None = None,
    ) -> ObjectInfo: ...

    def put(
        self,
        key: str,
        source: BinaryIO,
        *,
        content_type: str = "application/octet-stream",
        max_bytes: int = DEFAULT_MAX_BYTES,
        expected_sha256: str | None = None,
    ) -> ObjectInfo: ...

    def open(self, key: str) -> BinaryIO: ...
    def stat(self, key: str) -> ObjectInfo: ...
    def delete(self, key: str) -> None: ...
    def presign_download(
        self,
        key: str,
        *,
        expires_seconds: int = 300,
        download_filename: str | None = None,
    ) -> SecretURL: ...
    def presign_upload(
        self, key: str, *, content_type: str, expires_seconds: int = 300
    ) -> SecretURL: ...
    def presign_worker_upload(
        self, key: str, *, content_type: str, expires_seconds: int = 300
    ) -> SecretURL: ...
    def presign_artifact_upload(
        self,
        key: str,
        *,
        content_type: str,
        size_bytes: int,
        content_md5: str,
        expires_seconds: int = 300,
    ) -> SecretURL: ...


def _no_links(path: Path, *, missing_ok: bool = False):
    """Reject symlinks and Windows reparse points, including junctions."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        if missing_ok:
            return None
        raise ObjectNotFound("Stored object was not found") from None
    if stat_module.S_ISLNK(info.st_mode) or getattr(
        info, "st_file_attributes", 0
    ) & getattr(stat_module, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
        raise StorageError("Storage paths must not contain links or reparse points")
    return info


def _check_ancestors(path: Path) -> None:
    for ancestor in reversed((path, *path.parents)):
        _no_links(ancestor, missing_ok=True)


def _sync_directory(path: Path) -> None:
    if os.name != "nt":
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


class LocalObjectStore:
    """Server-owned root. Immutable publish uses an atomic directory rename.

    Keys are mapped to a flat SHA256 directory name, never joined as filesystem
    paths. Do not give other OS users write access to this root; link checks do
    not claim protection from an administrator racing filesystem operations.
    """

    capabilities = LOCAL_CAPABILITIES

    def __init__(self, root: Path | str):
        root = Path(root)
        if not root.is_absolute() or ".." in root.parts:
            raise StorageConfigurationError(
                "Local object storage needs an explicit absolute root"
            )
        _check_ancestors(root)
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root = root.resolve(strict=True)
        self._objects, self._staging = self.root / "objects", self.root / "staging"
        for path in (self._objects, self._staging):
            _no_links(path, missing_ok=True)
            path.mkdir(exist_ok=True, mode=0o700)

    def _directory(self, key: str) -> Path:
        validate_key(key)
        _check_ancestors(self.root)
        _no_links(self._objects)
        return self._objects / hashlib.sha256(key.encode("ascii")).hexdigest()

    @staticmethod
    def _read_file(path: Path):
        before = _no_links(path)
        if not stat_module.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise StorageError("Stored object must be a regular private file")
        fd = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        after = os.fstat(fd)
        if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
            os.close(fd)
            raise StorageError("Storage path changed during access")
        return os.fdopen(fd, "rb")

    def write_new(
        self,
        owner_id: str,
        asset_id: str,
        source: BinaryIO,
        *,
        filename="blob",
        content_type="application/octet-stream",
        max_bytes=DEFAULT_MAX_BYTES,
        expected_sha256=None,
    ) -> ObjectInfo:
        return self.put(
            make_object_key(owner_id, asset_id, filename),
            source,
            content_type=content_type,
            max_bytes=max_bytes,
            expected_sha256=expected_sha256,
        )

    def put(
        self,
        key: str,
        source: BinaryIO,
        *,
        content_type="application/octet-stream",
        max_bytes=DEFAULT_MAX_BYTES,
        expected_sha256=None,
    ) -> ObjectInfo:
        _content_type(content_type)
        _limits(max_bytes, expected_sha256)
        target = self._directory(key)
        if _no_links(target, missing_ok=True) is not None:
            raise ObjectAlreadyExists("An object already exists at this key")
        _no_links(self._staging)
        with tempfile.TemporaryDirectory(
            prefix="upload-", dir=self._staging
        ) as temporary:
            staged = Path(temporary) / "publish"
            staged.mkdir(mode=0o700)
            with (staged / "blob").open("xb") as handle:
                size, checksum = _copy(source, handle, max_bytes, expected_sha256)
                handle.flush()
                os.fsync(handle.fileno())
            result = ObjectInfo("local", key, size, checksum, content_type, checksum)
            with (staged / "meta.json").open("x", encoding="utf-8") as handle:
                json.dump(
                    asdict(result), handle, ensure_ascii=True, separators=(",", ":")
                )
                handle.flush()
                os.fsync(handle.fileno())
            _sync_directory(staged)
            self._directory(key)
            if _no_links(target, missing_ok=True) is not None:
                raise ObjectAlreadyExists("An object already exists at this key")
            try:
                os.rename(staged, target)
            except OSError:
                if target.exists():
                    raise ObjectAlreadyExists(
                        "An object already exists at this key"
                    ) from None
                raise StorageError("Could not atomically publish the object") from None
            try:
                _sync_directory(self._objects)
            except OSError:
                raise StorageWriteUncertain(key) from None
        return result

    def stat(self, key: str) -> ObjectInfo:
        directory = self._directory(key)
        _no_links(directory)
        with self._read_file(directory / "meta.json") as handle:
            raw = handle.read(4097)
        if len(raw) > 4096:
            raise IntegrityError("Stored object metadata is invalid")
        try:
            result = ObjectInfo(**json.loads(raw))
            if (
                result.provider != "local"
                or result.key != key
                or type(result.size_bytes) is not int
                or result.size_bytes < 0
                or not isinstance(result.sha256, str)
                or not _HASH.fullmatch(result.sha256)
            ):
                raise ValueError
            _content_type(result.content_type)
        except (ValueError, TypeError, StorageError):
            raise IntegrityError("Stored object metadata is invalid") from None
        blob = _no_links(directory / "blob")
        if (
            not stat_module.S_ISREG(blob.st_mode)
            or blob.st_nlink != 1
            or blob.st_size != result.size_bytes
        ):
            raise IntegrityError("Stored object size or file type changed")
        return result

    def open(self, key: str) -> BinaryIO:
        self.stat(key)
        directory = self._directory(key)
        _no_links(directory)
        return self._read_file(directory / "blob")

    def delete(self, key: str) -> None:
        directory = self._directory(key)
        if _no_links(directory, missing_ok=True) is None:
            return
        self.stat(key)
        if {item.name for item in directory.iterdir()} != {"blob", "meta.json"}:
            raise StorageError("Unexpected files in object directory; deletion refused")
        # No recursive deletion, and no following links or caller-supplied paths.
        (directory / "blob").unlink()
        (directory / "meta.json").unlink()
        directory.rmdir()

    def presign_download(
        self, key: str, *, expires_seconds=300, download_filename=None
    ) -> SecretURL:
        validate_key(key)
        raise UnsupportedStorageOperation(
            "Local objects require an authenticated application download route"
        )

    def presign_worker_upload(
        self, key: str, *, content_type: str, expires_seconds=300
    ) -> SecretURL:
        validate_key(key)
        raise UnsupportedStorageOperation(
            "Local objects cannot be written by a remote worker"
        )

    def presign_upload(
        self, key: str, *, content_type: str, expires_seconds=300
    ) -> SecretURL:
        validate_key(key)
        raise UnsupportedStorageOperation(
            "Local uploads require an authenticated application upload route"
        )

    def presign_artifact_upload(
        self,
        key: str,
        *,
        content_type: str,
        size_bytes: int,
        content_md5: str,
        expires_seconds=300,
    ) -> SecretURL:
        raise UnsupportedStorageOperation(
            "GPU artifact delivery requires the selected R2 store"
        )


class _SafeBody:
    """Close remote streaming bodies and suppress signed URLs in transport errors."""

    def __init__(self, body):
        self._body = body

    def read(self, size=-1):
        try:
            return self._body.read(size)
        except Exception:
            raise StorageError(
                "Object download failed; retry collection, not generation"
            ) from None

    def close(self):
        try:
            self._body.close()
        except Exception:
            raise StorageError(
                "Object download connection could not be closed"
            ) from None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


class S3ObjectStore:
    """S3/R2 plus an experimental Hippius transport; never falls back to another store.

    Fixed-key put requires server-side atomic create. Hippius only accepts
    write_new with adapter-generated UUID keys; this is not provider-enforced
    immutability. PUTs are bounded to 100 MiB; larger resumable R2/AWS uploads
    require the explicitly constructed, durable MultipartUploadManager.
    """

    def __init__(
        self,
        config: S3StorageConfig,
        credentials: S3Credentials | None = None,
        *,
        client=None,
    ):
        if not config.enabled:
            raise StorageConfigurationError("Cloud storage is disabled")
        self.config, self.capabilities = config, config.capabilities
        if client is None:
            if not isinstance(credentials, S3Credentials):
                raise StorageConfigurationError(
                    "Explicit storage credentials are required"
                )
            if config.provider == "hippius" and not credentials._access.startswith(
                "hip_"
            ):
                raise StorageConfigurationError(
                    "Hippius requires an S3 access key, not wallet credentials"
                )
            try:
                import boto3
                from botocore.config import Config

                session = boto3.Session(
                    aws_access_key_id=credentials._access,
                    aws_secret_access_key=credentials._secret,
                    aws_session_token=credentials._token,
                    region_name=config.region,
                )
                client = session.client(
                    "s3",
                    endpoint_url=config.endpoint_url,
                    region_name=config.region,
                    verify=True,
                    config=Config(
                        signature_version="s3v4",
                        s3={"addressing_style": "path"},
                        ignore_configured_endpoint_urls=True,
                        proxies={},
                        retries={"total_max_attempts": 1, "mode": "standard"},
                        connect_timeout=10,
                        read_timeout=120,
                        max_pool_connections=16,
                        request_checksum_calculation="when_required",
                        response_checksum_validation="when_required",
                    ),
                )
            except Exception:
                raise StorageConfigurationError(
                    "Could not initialize the explicit S3 client"
                ) from None
        # Injected clients are a trusted integration/test seam, never user input.
        meta = getattr(client, "meta", None)
        if (
            meta is None
            or getattr(meta, "endpoint_url", None) != config.endpoint_url
            or getattr(meta, "region_name", None) != config.region
        ):
            raise StorageConfigurationError(
                "S3 client endpoint or region differs from explicit configuration"
            )
        events = getattr(meta, "events", None)
        if events is not None:
            # Reject SDK region/redirect middleware changing the configured host.
            events.register_first("before-send.s3", self._guard_endpoint)
        self._client = client
        self._video_credentials = (
            credentials if isinstance(credentials, S3Credentials) else None
        )

    def _guard_endpoint(self, request, **kwargs):
        try:
            target, allowed = urlsplit(request.url), urlsplit(self.config.endpoint_url)
            valid = target.scheme == "https" and target.netloc == allowed.netloc
        except (AttributeError, ValueError):
            valid = False
        if not valid:
            raise StorageError("Storage SDK attempted to change the selected endpoint")

    def _call(self, operation: str, key: str, **kwargs):
        validate_key(key)
        try:
            return getattr(self._client, operation)(
                Bucket=self.config.bucket, Key=key, **kwargs
            )
        except Exception as error:
            # This transport boundary translates SDK errors without serializing
            # raw responses, exception text, Authorization or query strings.
            response = getattr(error, "response", {})
            response = response if isinstance(response, dict) else {}
            details = response.get("Error", {})
            details = details if isinstance(details, dict) else {}
            metadata = response.get("ResponseMetadata", {})
            status = (
                metadata.get("HTTPStatusCode") if isinstance(metadata, dict) else None
            )
            code = details.get("Code")
            if code in ("NoSuchKey", "NotFound", "404") or status == 404:
                raise ObjectNotFound("Stored object was not found") from None
            if code in (
                "PreconditionFailed",
                "ConditionalRequestConflict",
            ) or status in (409, 412):
                raise ObjectAlreadyExists(
                    "Object key is occupied or has a concurrent write"
                ) from None
            if operation == "put_object" and (
                not isinstance(status, int) or status >= 500
            ):
                raise StorageWriteUncertain(key) from None
            raise StorageError("Storage request failed") from None

    def write_new(
        self,
        owner_id: str,
        asset_id: str,
        source: BinaryIO,
        *,
        filename="blob",
        content_type="application/octet-stream",
        max_bytes=DEFAULT_MAX_BYTES,
        expected_sha256=None,
    ) -> ObjectInfo:
        key = make_object_key(owner_id, asset_id, filename)
        return self._put(
            key,
            source,
            content_type,
            max_bytes,
            expected_sha256,
            conditional=self.capabilities.conditional_create,
        )

    def put(
        self,
        key: str,
        source: BinaryIO,
        *,
        content_type="application/octet-stream",
        max_bytes=DEFAULT_MAX_BYTES,
        expected_sha256=None,
    ) -> ObjectInfo:
        validate_key(key)
        if not self.capabilities.conditional_create:
            raise UnsupportedStorageOperation(
                "Provider cannot atomically reject overwrites; use write_new"
            )
        return self._put(
            key, source, content_type, max_bytes, expected_sha256, conditional=True
        )

    def _put(
        self, key, source, content_type, max_bytes, expected_sha256, *, conditional
    ):
        _content_type(content_type)
        _limits(max_bytes, expected_sha256)
        limit = min(max_bytes, self.config.max_single_put_bytes)
        with tempfile.SpooledTemporaryFile(
            max_size=8 * 1024 * 1024, mode="w+b"
        ) as staged:
            size, checksum = _copy(source, staged, limit, expected_sha256)
            staged.seek(0)
            extra = {"IfNoneMatch": "*"} if conditional else {}
            response = self._call(
                "put_object",
                key,
                Body=staged,
                ContentLength=size,
                ContentType=content_type,
                Metadata={"sha256": checksum},
                **extra,
            )
        return ObjectInfo(
            self.config.provider,
            key,
            size,
            checksum,
            content_type,
            _identifier(response.get("ETag")),
            _identifier(response.get("VersionId")),
        )

    def stat(self, key: str) -> ObjectInfo:
        response = self._call("head_object", key)
        if not isinstance(response, dict):
            raise IntegrityError("Provider returned invalid object metadata")
        size = response.get("ContentLength")
        if type(size) is not int or size < 0:
            raise IntegrityError("Provider returned invalid object metadata")
        metadata = response.get("Metadata", {})
        checksum = metadata.get("sha256") if isinstance(metadata, dict) else None
        checksum = (
            checksum
            if isinstance(checksum, str) and _HASH.fullmatch(checksum)
            else None
        )
        return ObjectInfo(
            self.config.provider,
            key,
            size,
            checksum,
            _content_type(response.get("ContentType", "application/octet-stream")),
            _identifier(response.get("ETag")),
            _identifier(response.get("VersionId")),
        )

    def open(self, key: str) -> BinaryIO:
        response = self._call("get_object", key)
        if not hasattr(response.get("Body"), "read"):
            raise StorageError("Provider did not return a readable object")
        return _SafeBody(response["Body"])

    def delete(self, key: str) -> None:
        self._call("delete_object", key)

    def _download_disposition(self, filename):
        if filename is None:
            return None
        if (
            not isinstance(filename, str)
            or not 1 <= len(filename) <= 255
            or any(ord(c) < 32 or ord(c) == 127 or c in "/\\" for c in filename)
        ):
            raise StorageError("Invalid download filename")
        if self.config.provider == "hippius":
            raise UnsupportedStorageOperation(
                "Attachment response override has not been qualified for Hippius"
            )
        return "attachment; filename*=UTF-8''" + quote(filename, safe="")

    def presign_download(
        self, key: str, *, expires_seconds=300, download_filename=None
    ) -> SecretURL:
        disposition = self._download_disposition(download_filename)
        return self._presign(key, "GET", expires_seconds, {}, disposition=disposition)

    def presign_immutable_video(
        self, key: str, *, content_type: str, download_filename=None
    ) -> SecretURL:
        """Only the authorized immutable artifact-video route uses this R2 policy.

        One request-local signer changes its own timestamp before canonicalization;
        ordinary asset GET/PUT signers and the process clock remain untouched.
        Pinned source: https://github.com/boto/botocore/blob/1.43.108/botocore/auth.py,
        SigV4Auth.add_auth -> S3SigV4QueryAuth._modify_request_before_signing.
        """
        if content_type != "video/mp4":
            raise StorageError("Immutable video caching requires an MP4 artifact")
        return self._presign_immutable_media(
            key, content_type=content_type, download_filename=download_filename
        )

    def presign_immutable_rendition(
        self, key: str, *, content_type: str, download_filename=None
    ) -> SecretURL:
        """Only a verified owner-authorized display rendition may use this signer."""
        if content_type != "image/webp":
            raise StorageError(
                "Immutable rendition caching requires a WebP display copy"
            )
        return self._presign_immutable_media(
            key, content_type=content_type, download_filename=download_filename
        )

    def presign_immutable_media(
        self, key: str, *, content_type: str, download_filename=None
    ) -> SecretURL:
        """Owner-authorized, hash-verified media bytes (uploads and result
        images/audio). The bucketed URL lets the browser reuse its copy instead
        of downloading the same object again on every view."""
        if (
            not isinstance(content_type, str)
            or not _MIME.fullmatch(content_type)
            or content_type.split("/")[0] not in {"image", "video", "audio"}
        ):
            raise StorageError("Immutable media caching requires image, video or audio")
        return self._presign_immutable_media(
            key, content_type=content_type, download_filename=download_filename
        )

    def _presign_immutable_media(self, key, *, content_type, download_filename):
        validate_key(key)
        if self.config.provider != "r2":
            raise UnsupportedStorageOperation(
                "Immutable media caching requires the selected R2 store"
            )
        disposition = self._download_disposition(download_filename) or "inline"
        if self._video_credentials is None:
            raise StorageError(
                "Explicit storage credentials are required for immutable video signing"
            )
        now = int(time.time())
        bucket = now // VIDEO_SIGNING_BUCKET_SECONDS * VIDEO_SIGNING_BUCKET_SECONDS
        try:
            from botocore.auth import S3SigV4QueryAuth
            from botocore.awsrequest import AWSRequest
            from botocore.credentials import Credentials

            class BucketedVideoAuth(S3SigV4QueryAuth):
                def _modify_request_before_signing(self, request):
                    request.context["timestamp"] = datetime.fromtimestamp(
                        bucket, timezone.utc
                    ).strftime("%Y%m%dT%H%M%SZ")
                    super()._modify_request_before_signing(request)

            credentials = self._video_credentials
            frozen = Credentials(
                credentials._access, credentials._secret, credentials._token
            ).get_frozen_credentials()
            request = AWSRequest(
                method="GET",
                url=f"{self.config.endpoint_url}/{self.config.bucket}/{key}",
                params={
                    "response-cache-control": VIDEO_OBJECT_CACHE_CONTROL,
                    "response-content-type": content_type,
                    "response-content-disposition": disposition,
                },
            )
            BucketedVideoAuth(
                frozen, "s3", self.config.region, expires=VIDEO_SIGNING_VALIDITY_SECONDS
            ).add_auth(request)
            url = request.url
            parsed, endpoint = urlsplit(url), urlsplit(self.config.endpoint_url)
            if (
                parsed.scheme != "https"
                or parsed.netloc != endpoint.netloc
                or parsed.fragment
                or parsed.path != f"/{self.config.bucket}/{key}"
                or not parsed.query
            ):
                raise ValueError
        except Exception:
            raise StorageError(
                "Could not create immutable video access for the selected storage endpoint"
            ) from None
        return SecretURL(url, "GET", bucket + VIDEO_SIGNING_VALIDITY_SECONDS, {})

    def presign_upload(
        self, key: str, *, content_type: str, expires_seconds=300
    ) -> SecretURL:
        if not self.capabilities.presigned_upload:
            raise UnsupportedStorageOperation(
                "Browser direct upload is not enabled for this provider"
            )
        return self._presign(
            key,
            "PUT",
            expires_seconds,
            {"Content-Type": _content_type(content_type), "If-None-Match": "*"},
        )

    def presign_worker_upload(
        self, key: str, *, content_type: str, expires_seconds=300
    ) -> SecretURL:
        """A PUT a remote worker can send with only Content-Type (#795).

        Plain HTTP clients don't add If-None-Match, so it is not signed here;
        callers must use a key unique to one attempt and never reused.
        """
        return self._presign(
            key, "PUT", expires_seconds, {"Content-Type": _content_type(content_type)}
        )

    def presign_artifact_upload(
        self,
        key: str,
        *,
        content_type: str,
        size_bytes: int,
        content_md5: str,
        expires_seconds=300,
    ) -> SecretURL:
        """Authorize one immutable GPU output; no credentials leave this process.

        This explicit artifact limit does not widen browser/direct PUT limits.
        MD5 is transport integrity only; collection still verifies full SHA-256.
        """
        if (
            self.config.provider != "r2"
            or not self.capabilities.conditional_create
            or not self.capabilities.presigned_upload
        ):
            raise UnsupportedStorageOperation(
                "GPU artifact delivery requires the selected R2 store"
            )
        if (
            content_type not in {"video/mp4", "audio/flac"}
            or type(size_bytes) is not int
            or not 1 <= size_bytes <= 512 * 1024 * 1024
            or type(expires_seconds) is not int
            or not 1 <= expires_seconds <= 300
        ):
            raise StorageError("Invalid GPU artifact upload constraints")
        try:
            checksum = base64.b64decode(content_md5, validate=True)
            if (
                len(checksum) != 16
                or base64.b64encode(checksum).decode("ascii") != content_md5
            ):
                raise ValueError
        except (TypeError, ValueError):
            raise StorageError("Invalid GPU artifact upload checksum") from None
        return self._presign(
            key,
            "PUT",
            expires_seconds,
            {
                "Content-Type": content_type,
                "If-None-Match": "*",
                "Content-Length": str(size_bytes),
                "Content-MD5": content_md5,
            },
        )

    def _presign(self, key, method, expires_seconds, headers, *, disposition=None):
        validate_key(key)
        # Application policy deliberately shorter than the providers' maximum.
        if type(expires_seconds) is not int or not 1 <= expires_seconds <= 3600:
            raise StorageError(
                "Signed object access must expire within 1 to 3600 seconds"
            )
        params = {"Bucket": self.config.bucket, "Key": key}
        if disposition is not None:
            if method != "GET":
                raise StorageError(
                    "Response disposition is only supported for downloads"
                )
            params["ResponseContentDisposition"] = disposition
        if method == "PUT":
            params.update(ContentType=headers["Content-Type"], IfNoneMatch="*")
            if "Content-Length" in headers:
                params["ContentLength"] = int(headers["Content-Length"])
            if "Content-MD5" in headers:
                params["ContentMD5"] = headers["Content-MD5"]
        try:
            url = self._client.generate_presigned_url(
                "get_object" if method == "GET" else "put_object",
                Params=params,
                ExpiresIn=expires_seconds,
                HttpMethod=method,
            )
            parsed, endpoint = urlsplit(url), urlsplit(self.config.endpoint_url)
            if (
                parsed.scheme != "https"
                or parsed.netloc != endpoint.netloc
                or parsed.fragment
                or parsed.path != f"/{self.config.bucket}/{key}"
                or not parsed.query
            ):
                raise ValueError
        except Exception:
            raise StorageError(
                "Could not create access for the selected storage endpoint"
            ) from None
        return SecretURL(url, method, time.time() + expires_seconds, headers)


def _identifier(value):
    # ETags/version IDs are opaque identifiers, never URLs or provider errors.
    if value is None:
        return None
    if isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9._+\-/="-]{1,256}', value):
        return value
    raise IntegrityError("Provider returned an invalid object identifier")
