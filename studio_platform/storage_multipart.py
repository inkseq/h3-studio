"""Resumable server-mediated uploads for reviewed R2/AWS S3 backends.

No client, credential or network is created on import. Journal construction only
creates its own table in the supplied database. Caller authenticates owner and
asset, reserves quota, and persists the session ID. Signed URLs are never stored.
This manager intentionally does not provide direct browser part signing yet.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import re
import tempfile
import time
import uuid
from dataclasses import asdict
from typing import BinaryIO

from sqlalchemy import (
    Column,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    insert,
    select,
    update,
)
from sqlalchemy.exc import IntegrityError as SQLIntegrityError
from .storage_schema import create_storage_schema

from .storage import (
    IntegrityError,
    ObjectInfo,
    ObjectNotFound,
    ObjectTooLarge,
    S3ObjectStore,
    StorageError,
    UnsupportedStorageOperation,
    _content_type,
    _copy,
    _identifier,
    _limits,
    _part,
    make_object_key,
    validate_key,
)

MIB = 1024 * 1024
_metadata = MetaData()
_uploads = Table(
    "storage_multipart_uploads",
    _metadata,
    Column("id", String(32), primary_key=True),
    Column("tenant", String(128), nullable=False),
    Column("owner", String(128), nullable=False),
    Column("request_key", String(128), nullable=False),
    Column("request_hash", String(64), nullable=False),
    Column("version", Integer, nullable=False),
    Column("record", Text, nullable=False),
    UniqueConstraint(
        "tenant", "owner", "request_key", name="uq_storage_upload_request"
    ),
)
_fixed_keys = Table(
    "storage_multipart_fixed_keys",
    _metadata,
    Column("identity", String(64), primary_key=True),
    Column("upload_id", String(32), nullable=False),
)


class MultipartConflict(StorageError):
    pass


class MultipartOutcomeUnknown(StorageError):
    """Keep this session/key. Reconcile it; do not start a replacement upload."""

    def __init__(self, session_id: str, key: str):
        self.session_id, self.key = session_id, key
        super().__init__(
            "Upload outcome is unknown; reconcile the existing upload session"
        )


def _hash(value) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _upload_id(value) -> str:
    if not isinstance(value, str) or not re.fullmatch(
        r"[A-Za-z0-9._+=/\-]{1,2048}", value
    ):
        raise IntegrityError("Provider returned an invalid multipart identifier")
    return value


class MultipartJournal:
    """Durable optimistic updates; engine must point to a persistent application DB."""

    def __init__(self, engine, tenant="sixnine"):
        self.engine, self.tenant = engine, _part(tenant)
        create_storage_schema(engine, _metadata)

    def _decode(self, row):
        result = json.loads(row.record)
        result["version"] = row.version
        return result

    def get(self, owner_id: str, session_id: str) -> dict:
        _part(owner_id)
        if not isinstance(session_id, str) or not re.fullmatch(
            r"[0-9a-f]{32}", session_id
        ):
            raise ObjectNotFound("Upload session was not found")
        with self.engine.connect() as conn:
            row = conn.execute(
                select(_uploads).where(
                    _uploads.c.id == session_id,
                    _uploads.c.tenant == self.tenant,
                    _uploads.c.owner == owner_id,
                )
            ).first()
        if row is None:
            raise ObjectNotFound("Upload session was not found")
        return self._decode(row)

    def create(
        self, owner_id: str, request_key: str, request_hash: str, record: dict
    ) -> tuple[dict, bool]:
        _part(owner_id)
        _part(request_key)
        try:
            with self.engine.begin() as conn:
                if "object_key" in record:
                    conn.execute(
                        insert(_fixed_keys).values(
                            identity=_hash([record["binding"], record["key"]]),
                            upload_id=record["id"],
                        )
                    )
                conn.execute(
                    insert(_uploads).values(
                        id=record["id"],
                        tenant=self.tenant,
                        owner=owner_id,
                        request_key=request_key,
                        request_hash=request_hash,
                        version=0,
                        record=json.dumps(record),
                    )
                )
            return dict(record, version=0), True
        except SQLIntegrityError:
            with self.engine.connect() as conn:
                row = conn.execute(
                    select(_uploads).where(
                        _uploads.c.tenant == self.tenant,
                        _uploads.c.owner == owner_id,
                        _uploads.c.request_key == request_key,
                    )
                ).first()
            if row is None or row.request_hash != request_hash:
                raise MultipartConflict(
                    "Upload request key already has different parameters"
                ) from None
            return self._decode(row), False

    def save(self, owner_id: str, record: dict) -> dict:
        # A CAS prevents two processes from submitting the same operation intent.
        value = dict(record)
        version = value.pop("version")
        value["updated_at"] = time.time()
        with self.engine.begin() as conn:
            result = conn.execute(
                update(_uploads)
                .where(
                    _uploads.c.id == record["id"],
                    _uploads.c.tenant == self.tenant,
                    _uploads.c.owner == owner_id,
                    _uploads.c.version == version,
                )
                .values(version=version + 1, record=json.dumps(value))
            )
            if result.rowcount != 1:
                raise MultipartConflict(
                    "Upload session changed; reload its current status"
                )
        return dict(value, version=version + 1)


class MultipartUploadManager:
    """One in-flight operation per session; independent sessions can run concurrently.

    Defaults (application policy): 512 MiB objects and 32 MiB parts. Unlike
    S3ObjectStore.put, multipart defaults to a new UUID key. An internal caller
    may supply its durably reserved attempt key. AWS conditional complete is
    enforced; R2 relies on caller ownership and a unique journal, not a provider
    promise of conditional completion.
    Full readback verifies bytes before completed. No billing/transfer is automatic.
    """

    def __init__(
        self,
        store: S3ObjectStore,
        journal: MultipartJournal,
        *,
        max_object_bytes=512 * MIB,
        part_size=32 * MIB,
    ):
        if not isinstance(store, S3ObjectStore) or store.config.provider not in (
            "r2",
            "aws-s3",
        ):
            raise UnsupportedStorageOperation(
                "Multipart is enabled only for reviewed R2 and AWS S3 backends"
            )
        if type(part_size) is not int or not 5 * MIB <= part_size <= 100 * MIB:
            raise StorageError("Multipart part size must be between 5 and 100 MiB")
        if type(max_object_bytes) is not int or not 1 <= max_object_bytes <= 512 * MIB:
            raise StorageError(
                "This application supports multipart objects up to 512 MiB"
            )
        self.store, self.journal = store, journal
        self.max_object_bytes, self.part_size = max_object_bytes, part_size
        config = store.config
        self.binding = _hash(
            {
                k: getattr(config, k)
                for k in (
                    "provider",
                    "endpoint_url",
                    "region",
                    "bucket",
                    "service",
                    "profile",
                )
            }
        )

    @property
    def capabilities(self):
        return {
            "server_multipart": True,
            "browser_multipart": False,
            "restart_reconciliation": True,
            "max_object_bytes": self.max_object_bytes,
            "part_size": self.part_size,
            "conditional_complete": self.store.config.provider == "aws-s3",
        }

    def _get(self, owner_id, session_id):
        record = self.journal.get(owner_id, session_id)
        if record["binding"] != self.binding:
            raise MultipartConflict(
                "Upload belongs to a different provider, bucket or profile"
            )
        return record

    @staticmethod
    def public_status(record):
        # Provider upload IDs, physical keys and secrets never enter the public status.
        return {
            name: record[name]
            for name in (
                "id",
                "asset_id",
                "status",
                "size_bytes",
                "part_size",
                "part_count",
            )
        } | {
            "completed_parts": sorted(
                int(n)
                for n, part in record["parts"].items()
                if part["status"] == "done"
            )
        }

    def get(self, owner_id, session_id):
        return self.public_status(self._get(owner_id, session_id))

    def begin(
        self,
        owner_id: str,
        asset_id: str,
        request_key: str,
        *,
        size_bytes: int,
        sha256: str,
        filename="blob",
        content_type="application/octet-stream",
        object_key=None,
    ) -> dict:
        _limits(self.max_object_bytes, sha256)
        if sha256 is None:
            raise IntegrityError("Multipart requires an expected whole-object SHA256")
        if type(size_bytes) is not int or not 1 <= size_bytes <= self.max_object_bytes:
            raise ObjectTooLarge(
                "Object exceeds the effective multipart size limit or is empty"
            )
        _content_type(content_type)
        key = make_object_key(owner_id, asset_id, filename)
        request = dict(
            asset_id=asset_id,
            size_bytes=size_bytes,
            sha256=sha256,
            filename=filename,
            content_type=content_type,
            binding=self.binding,
        )
        if object_key is not None:
            key = validate_key(object_key)
            if not key.startswith(
                f"owners/{_part(owner_id)}/assets/{_part(asset_id)}/"
            ):
                raise MultipartConflict(
                    "Fixed upload key does not belong to this owner and attempt"
                )
            # Keep the old fingerprint for callers using the original signature.
            request["object_key"] = key
        record = dict(
            request,
            id=uuid.uuid4().hex,
            key=key,
            status="planned",
            parts={},
            part_size=self.part_size,
            part_count=math.ceil(size_bytes / self.part_size),
            updated_at=time.time(),
        )
        record, created = self.journal.create(
            owner_id, request_key, _hash(request), record
        )
        if not created and record["status"] != "planned":
            return self.public_status(record)
        # A process may stop after journal insertion but before creating intent.
        # In planned state no provider operation has started; CAS elects one caller.
        record["status"] = "creating"
        try:
            record = self.journal.save(owner_id, record)
        except MultipartConflict:
            return self.get(owner_id, record["id"])
        try:
            result = self.store._call(
                "create_multipart_upload",
                record["key"],
                ContentType=content_type,
                Metadata={"sha256": sha256, "upload-session": record["id"]},
            )
            record["upload_id"] = _upload_id(result.get("UploadId"))
        except Exception:
            self._unknown(owner_id, record, "creation_unknown")
        record["status"] = "active"
        record = self.journal.save(owner_id, record)
        return self.public_status(record)

    def _unknown(self, owner_id, record, status):
        record["status"] = status
        self.journal.save(owner_id, record)
        raise MultipartOutcomeUnknown(record["id"], record["key"]) from None

    def _part_length(self, record, number):
        if type(number) is not int or not 1 <= number <= record["part_count"]:
            raise StorageError("Multipart part number is outside this upload")
        return min(
            record["part_size"],
            record["size_bytes"] - (number - 1) * record["part_size"],
        )

    def upload_part(
        self, owner_id: str, session_id: str, part_number: int, source: BinaryIO
    ) -> dict:
        record = self._get(owner_id, session_id)
        length = self._part_length(record, part_number)
        if record["status"] != "active":
            raise MultipartConflict(
                "Upload is not ready for another part; reconcile its current status"
            )
        with tempfile.SpooledTemporaryFile(max_size=8 * MIB, mode="w+b") as staged:
            size, sha = _copy(source, staged, length, None)
            if size != length:
                raise IntegrityError("Multipart part has an incorrect byte length")
            old = record["parts"].get(str(part_number))
            if old and old["sha256"] != sha:
                raise MultipartConflict(
                    "A part number cannot be reused with different bytes"
                )
            if old and old["status"] == "done":
                return self.public_status(record)
            staged.seek(0)
            md5 = hashlib.md5(usedforsecurity=False)
            while chunk := staged.read(MIB):
                md5.update(chunk)
            part = {
                "sha256": sha,
                "size_bytes": size,
                "md5": md5.hexdigest(),
                "status": "uploading",
            }
            record["parts"][str(part_number)] = part
            record["status"], record["pending_part"] = "part_uploading", part_number
            record = self.journal.save(owner_id, record)
            staged.seek(0)
            try:
                result = self.store._call(
                    "upload_part",
                    record["key"],
                    UploadId=record["upload_id"],
                    PartNumber=part_number,
                    Body=staged,
                    ContentLength=size,
                    ContentMD5=base64.b64encode(md5.digest()).decode("ascii"),
                )
                etag = _identifier(result.get("ETag"))
                if etag is None:
                    raise IntegrityError("Provider did not return a part ETag")
            except Exception:
                self._unknown(owner_id, record, "part_unknown")
        record["parts"][str(part_number)].update(status="done", etag=etag)
        record.pop("pending_part", None)
        record["status"] = "active"
        return self.public_status(self.journal.save(owner_id, record))

    def _list_parts(self, record):
        parts, marker = {}, 0
        for _ in range(100):
            result = self.store._call(
                "list_parts",
                record["key"],
                UploadId=record["upload_id"],
                PartNumberMarker=marker,
            )
            for part in result.get("Parts", []):
                number, size, etag = (
                    part.get("PartNumber"),
                    part.get("Size"),
                    _identifier(part.get("ETag")),
                )
                if (
                    type(number) is not int
                    or number in parts
                    or type(size) is not int
                    or size < 0
                    or etag is None
                ):
                    raise IntegrityError("Provider returned invalid multipart metadata")
                parts[number] = {"size_bytes": size, "etag": etag}
            if not result.get("IsTruncated"):
                return parts
            next_marker = result.get("NextPartNumberMarker")
            if type(next_marker) is not int or next_marker <= marker:
                raise IntegrityError("Provider returned invalid multipart pagination")
            marker = next_marker
        raise StorageError("Multipart listing exceeded the bounded page limit")

    def _recover_creation(self, record):
        # ListMultipartUploads has no Key argument. Keep this narrow and redact
        # all SDK failures; do not use an unbounded auto paginator.
        found, markers = [], {}
        for _ in range(100):
            try:
                result = self.store._client.list_multipart_uploads(
                    Bucket=self.store.config.bucket, Prefix=record["key"], **markers
                )
            except Exception:
                raise StorageError(
                    "Could not reconcile the pending multipart creation"
                ) from None
            for upload in result.get("Uploads", []):
                if upload.get("Key") == record["key"]:
                    found.append(_upload_id(upload.get("UploadId")))
            if not result.get("IsTruncated"):
                break
            next_markers = {
                "KeyMarker": result.get("NextKeyMarker"),
                "UploadIdMarker": result.get("NextUploadIdMarker"),
            }
            if (
                not all(isinstance(v, str) for v in next_markers.values())
                or markers == next_markers
            ):
                raise IntegrityError("Provider returned invalid upload pagination")
            markers = next_markers
        else:
            raise StorageError("Upload listing exceeded the bounded page limit")
        if len(found) != 1:
            raise MultipartOutcomeUnknown(record["id"], record["key"])
        return found[0]

    def _verify_object(self, owner_id, record) -> ObjectInfo:
        # HEAD metadata is not proof of content. Always verify the exact bytes.
        info = self.store.stat(record["key"])
        if (
            info.size_bytes != record["size_bytes"]
            or info.content_type != record["content_type"]
        ):
            raise IntegrityError(
                "Completed upload metadata does not match its reserved asset"
            )
        digest, count = hashlib.sha256(), 0
        with self.store.open(record["key"]) as body:
            while chunk := body.read(min(MIB, record["size_bytes"] - count + 1)):
                count += len(chunk)
                if count > record["size_bytes"]:
                    raise IntegrityError(
                        "Completed upload exceeds its reserved byte length"
                    )
                digest.update(chunk)
        if count != record["size_bytes"] or digest.hexdigest() != record["sha256"]:
            raise IntegrityError(
                "Completed upload failed whole-object SHA256 verification"
            )
        info = ObjectInfo(
            info.provider,
            info.key,
            info.size_bytes,
            record["sha256"],
            info.content_type,
            info.etag,
            info.version_id,
        )
        record["status"], record["object"] = "completed", asdict(info)
        self.journal.save(owner_id, record)
        return info

    def complete(self, owner_id: str, session_id: str) -> ObjectInfo:
        record = self._get(owner_id, session_id)
        if record["status"] == "completed":
            return ObjectInfo(**record["object"])
        if record["status"] in ("completion_unknown", "verification_pending"):
            return self._verify_object(owner_id, record)
        if record["status"] != "active" or len(record["parts"]) != record["part_count"]:
            raise MultipartConflict("Every part must be uploaded before completion")
        try:
            remote = self._list_parts(record)
        except ObjectNotFound:
            # A concurrent winner can consume the MPU after our active snapshot
            # was read. Recheck the same owner/provider-bound journal, not HEAD:
            # only its completed state proves full byte verification occurred.
            current = self._get(owner_id, session_id)
            if current["version"] <= record["version"]:
                raise  # No state transition explains the genuinely missing MPU.
            if current["status"] == "completed":
                return ObjectInfo(**current["object"])
            # Completing, aborting and uncertain outcomes must stay recoverable;
            # never reissue Complete or infer success from an object's presence.
            raise MultipartConflict(
                "Upload session changed; reload its current status"
            ) from None
        expected = set(range(1, record["part_count"] + 1))
        if set(remote) != expected:
            raise MultipartConflict("Remote parts differ; reconcile before completion")
        manifest = []
        for number in sorted(expected):
            part, actual = record["parts"].get(str(number), {}), remote[number]
            if (
                part.get("status") != "done"
                or part.get("etag") != actual["etag"]
                or part.get("size_bytes") != actual["size_bytes"]
            ):
                raise MultipartConflict(
                    "Remote part changed; completion was not submitted"
                )
            manifest.append({"PartNumber": number, "ETag": actual["etag"]})
        record["status"] = "completing"
        record = self.journal.save(owner_id, record)
        # R2 documents multipart but not conditional complete. UUID key only.
        extra = {"IfNoneMatch": "*"} if self.store.config.provider == "aws-s3" else {}
        try:
            self.store._call(
                "complete_multipart_upload",
                record["key"],
                UploadId=record["upload_id"],
                MultipartUpload={"Parts": manifest},
                **extra,
            )
        except Exception:
            self._unknown(owner_id, record, "completion_unknown")
        record["status"] = "verification_pending"
        record = self.journal.save(owner_id, record)
        return self._verify_object(owner_id, record)

    def reconcile(self, owner_id: str, session_id: str, *, interrupted=False) -> dict:
        """Read remote progress. `interrupted=True` requires a fenced/stopped old worker.

        Never expose that flag directly to an HTTP client. A timer/expired lease
        alone cannot prove the original network operation has stopped.
        """
        record = self._get(owner_id, session_id)
        status = record["status"]
        pending = {
            "creating": "creation_unknown",
            "part_uploading": "part_unknown",
            "completing": "completion_unknown",
            "aborting": "abort_unknown",
        }
        if status in pending:
            if not interrupted:
                raise MultipartConflict(
                    "An operation is in flight; fence its worker before recovery"
                )
            status = record["status"] = pending[status]
            record = self.journal.save(owner_id, record)
        if status == "creation_unknown":
            record["upload_id"] = self._recover_creation(record)
            record["status"] = "active"
            record = self.journal.save(owner_id, record)
        elif status in ("active", "part_unknown"):
            remote = self._list_parts(record)
            if set(remote) - {int(n) for n in record["parts"]}:
                raise IntegrityError("Upload contains unreserved remote parts")
            for number, part in record["parts"].items():
                actual = remote.get(int(number))
                if actual is None:
                    part["status"] = "missing"
                    part.pop("etag", None)
                else:
                    if actual["size_bytes"] != part["size_bytes"]:
                        raise IntegrityError(
                            "Remote part length differs from the reserved bytes"
                        )
                    # ETag is opaque (AWS SSE/KMS may not be MD5); full SHA256 at
                    # complete is mandatory even when the lost response is found.
                    part.update(status="done", etag=actual["etag"])
            record["status"] = "active"
            record.pop("pending_part", None)
            record = self.journal.save(owner_id, record)
        elif status in ("completion_unknown", "verification_pending"):
            self._verify_object(owner_id, record)
            record = self._get(owner_id, session_id)
        elif status == "abort_unknown":
            try:
                self._list_parts(record)
            except ObjectNotFound:
                record["status"] = "aborted"
                record = self.journal.save(owner_id, record)
            else:
                raise MultipartOutcomeUnknown(record["id"], record["key"])
        return self.public_status(record)

    def abort(self, owner_id: str, session_id: str) -> dict:
        record = self._get(owner_id, session_id)
        if record["status"] == "aborted":
            return self.public_status(record)
        if record["status"] not in ("active", "planned"):
            raise MultipartConflict(
                "Cannot abort an in-flight or uncertain upload; reconcile it first"
            )
        if record["status"] == "planned":
            record["status"] = "aborted"
            return self.public_status(self.journal.save(owner_id, record))
        record["status"] = "aborting"
        record = self.journal.save(owner_id, record)
        try:
            self.store._call(
                "abort_multipart_upload", record["key"], UploadId=record["upload_id"]
            )
        except ObjectNotFound:
            pass
        except Exception:
            self._unknown(owner_id, record, "abort_unknown")
        record["status"] = "abort_unknown"
        self.journal.save(owner_id, record)
        return self.reconcile(owner_id, session_id)
