"""Private immutable assets with durable upload receipts and restart recovery."""

from __future__ import annotations

from dataclasses import asdict
from contextlib import contextmanager
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import time
import uuid

from sqlalchemy import Column, Float, MetaData, String, Table, Text, delete, select

from . import media
from .media_renditions import (
    ASSET_OBJECT_ROLES,
    RENDITIONS,
    POSTER_FRAME_BYTES,
    create_image_renditions,
    create_video_renditions,
    public_renditions,
)
from .storage import (
    IntegrityError,
    LocalObjectStore,
    ObjectAlreadyExists,
    ObjectInfo,
    S3ObjectStore,
    StorageWriteUncertain,
    UnsupportedStorageOperation,
    _check_ancestors,
    _copy,
    _no_links,
    _part,
    _sync_directory,
    make_object_key,
)
from .storage_asset_journal import (
    AssetUploadJournal,
    AssetConflict,
    AssetQuotaExceeded,  # noqa: F401 - public exception re-export used by asset callers.
    artifact_accounting,
    artifact_identity,
    receipts,
    validate_client_asset_id,
)
from .storage_multipart import (
    MultipartJournal,
    MultipartOutcomeUnknown,
    MultipartUploadManager,
)
from .storage_schema import create_storage_schema
from .asset_operation import asset_operation_lock
from . import scratch_retention
from .content_lifecycle import content_is_deleted, owner_reference_gate

MIB = 1024 * 1024
metadata = MetaData()
asset_table = Table(
    "platform_assets",
    metadata,
    Column("id", String(32), primary_key=True),
    Column("tenant", String(80), nullable=False),
    Column("owner", String(80), nullable=False),
    Column("project_id", String(160), nullable=False),
    Column("status", String(30), nullable=False),
    Column("created", Float, nullable=False),
    Column("record", Text, nullable=False),
)


class AssetNotFound(LookupError):
    pass


class _Sink:
    def write(self, value):
        return len(value)


class _PartReader:
    def __init__(self, source, size):
        self.source, self.remaining = source, size

    def read(self, size=-1):
        data = self.source.read(
            self.remaining if size < 0 else min(size, self.remaining)
        )
        self.remaining -= len(data)
        return data


class AssetService:
    # Suggested application caps, not supplier limits. Reservations include
    # original/model plus durable staging until verified publication commits.
    # Local and pre-cutover receipts keep their existing copies untouched.
    def __init__(
        self,
        engine,
        store,
        data_dir,
        *,
        tenant="sixnine",
        max_bytes=512 * MIB,
        owner_quota_bytes=10 * 1024 * MIB,
        tenant_quota_bytes=40 * 1024 * MIB,
        max_active_per_owner=2,
        max_active_total=4,
    ):
        self.engine, self.store, self.tenant, self.max_bytes = (
            engine,
            store,
            _part(tenant),
            max_bytes,
        )
        if type(max_bytes) is not int or not 1 <= max_bytes <= 512 * MIB:
            raise ValueError("素材单文件上限必须为1字节至512 MiB")
        if not store.capabilities.conditional_create:
            raise UnsupportedStorageOperation(
                "素材服务需要条件创建；Hippius实验适配器尚未接入素材服务"
            )
        if isinstance(store, S3ObjectStore):
            identity = {
                k: getattr(store.config, k)
                for k in (
                    "provider",
                    "endpoint_url",
                    "region",
                    "bucket",
                    "service",
                    "profile",
                )
            }
        elif isinstance(store, LocalObjectStore):
            identity = {"provider": "local", "root": str(store.root)}
        else:
            raise UnsupportedStorageOperation(
                "素材存储适配器尚未提供可验证的持久身份绑定"
            )
        self.storage_binding = hashlib.sha256(
            json.dumps(identity, sort_keys=True).encode()
        ).hexdigest()
        # Preserve both original binding formats; old receipts are not rewritten.
        self.artifact_storage_binding = hashlib.sha256(
            json.dumps(
                identity, sort_keys=True, separators=(",", ":"), allow_nan=False
            ).encode()
        ).hexdigest()
        root = Path(data_dir)
        if not root.is_absolute():
            raise ValueError("素材数据根必须为绝对路径")
        self.temp_dir, self.staging_dir = root / "processing", root / "asset-staging"
        self.operation_dir = root / "asset-operation-locks"
        for directory in (self.temp_dir, self.staging_dir, self.operation_dir):
            _check_ancestors(directory)
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        create_storage_schema(engine, metadata)
        self.journal = AssetUploadJournal(
            engine,
            asset_table,
            tenant=tenant,
            reservation_bytes=4 * max_bytes,
            owner_quota_bytes=owner_quota_bytes,
            tenant_quota_bytes=tenant_quota_bytes,
            max_active_per_owner=max_active_per_owner,
            max_active_total=max_active_total,
        )
        self.multipart = (
            MultipartUploadManager(
                store, MultipartJournal(engine, tenant), max_object_bytes=max_bytes
            )
            if isinstance(store, S3ObjectStore)
            else None
        )

    def usage(self, owner):
        return self.journal.usage(owner)

    def _stage(self, receipt):
        ident = receipt["id"]
        if len(ident) != 32 or any(c not in "0123456789abcdef" for c in ident):
            raise IntegrityError("素材暂存引用无效")
        directory = self.staging_dir / ident
        _check_ancestors(directory)
        return directory

    def _file(self, receipt, name):
        names = {
            "source" + receipt["suffix"],
            "normalized.png",
            "normalized.wav",
            "normalized.mp4",
            "receiving",
            "thumb-v1.webp",
            "preview-v1.webp",
            "poster-frame.png",
            "rendition-source" + receipt["suffix"],
            "rendition-receiving",
        }
        if receipt.get("derivation"):
            names.update(
                {receipt["derivation"]["input_name"], "trimmed.mp4", "trimmed.wav"}
            )
        if receipt.get("frame_extraction"):
            names.update({"artifact-input.mp4", "extracted.png"})
        if name not in names:
            raise IntegrityError("素材暂存文件名无效")
        path = self._stage(receipt) / name
        info = _no_links(path)
        if not path.is_file() or info.st_nlink != 1:
            raise IntegrityError("素材暂存文件类型无效")
        return path

    def _digest(self, path):
        with path.open("rb") as source:
            return _copy(source, _Sink(), self.max_bytes, None)

    def _reserve_to(self, receipt, required, *, renditions=False):
        previous = receipt["reserved"]
        if required <= previous:
            return
        receipt["reserved"] = required
        try:
            save = self.journal.save_renditions if renditions else self.journal.save
            save(receipt, byte_delta=required - previous)
        except Exception:
            receipt["reserved"] = (
                previous  # Failed SQL transaction did not charge these bytes.
            )
            raise

    def _stage_bytes(self, receipt):
        retained = 0
        directory = self._stage(receipt)
        if directory.exists():
            for path in directory.iterdir():
                info = _no_links(path)
                if not path.is_file() or info.st_nlink != 1:
                    raise IntegrityError("素材暂存出现未预期文件")
                retained += info.st_size
        return retained

    def _release(self, receipt, *, renditions=False):
        uncertain = {"putting", "put_unknown", "verifying", "multipart"}
        if any(s.get("phase") in uncertain for s in receipt["objects"].values()):
            retained = receipt["reserved"]
        else:
            retained = self._stage_bytes(receipt)
            retained += sum(
                receipt["asset"].get(role, {}).get("size_bytes", 0)
                for role in ASSET_OBJECT_ROLES
            )
        self.journal.release(receipt, retained, renditions=renditions)
        self._cleanup_after_release(receipt, renditions=renditions)

    def _cleanup_after_release(self, receipt, *, renditions=False):
        try:
            self._cleanup_ready(receipt, renditions=renditions)
        except Exception:
            # Cleanup is after acceptance: never report a ready upload as failed
            # or invite a duplicate submission because retained scratch remains.
            logging.getLogger(__name__).warning("r2_asset_scratch_cleanup_pending")

    def _cleanup_committed_asset(self, owner, asset_id):
        if (
            not isinstance(self.store, S3ObjectStore)
            or self.store.config.provider != "r2"
        ):
            return
        try:
            receipt = self.journal.get(owner, asset_id)
        except AssetConflict:
            # Historical published assets may have no upload receipt at all.
            return
        self._cleanup_after_release(receipt)

    def _cleanup_ready(self, receipt, *, renditions=False):
        if (
            not isinstance(self.store, S3ObjectStore)
            or self.store.config.provider != "r2"
            or receipt.get("scratch_policy") != scratch_retention.POLICY
            or receipt.get("storage_binding") != self.storage_binding
            or receipt["busy"]
            or receipt["asset"]["status"] != "ready"
        ):
            return
        cleanup = receipt.get("scratch_cleanup")
        if cleanup and cleanup["phase"] == "complete":
            return
        if not receipt.get("prepared") or "original" not in receipt["objects"]:
            raise IntegrityError("r2_asset_scratch_evidence_missing")
        for role, spec in receipt["objects"].items():
            actual = receipt["asset"].get(role, {})
            if (
                role not in ASSET_OBJECT_ROLES
                or spec.get("phase") != "verified"
                or any(
                    actual.get(k) != spec.get(k)
                    for k in ("key", "size_bytes", "sha256", "content_type")
                )
            ):
                raise IntegrityError("r2_asset_scratch_evidence_mismatch")
            self._verify_stored(spec)
        directory = self._stage(receipt)
        remote_bytes = sum(spec["size_bytes"] for spec in receipt["objects"].values())
        if cleanup is None:
            # A backfill may follow completed R2 scratch retention. Missing
            # previously removed originals are not recreated or deleted again.
            backfill = receipt.get("rendition_backfill")
            paths = [
                self._file(receipt, spec["filename"])
                for spec in receipt["objects"].values()
                if not backfill or (directory / spec["filename"]).exists()
            ]
            if backfill and (directory / backfill["source_name"]).exists():
                paths.append(self._file(receipt, backfill["source_name"]))
            if (directory / "poster-frame.png").exists():
                paths.append(self._file(receipt, "poster-frame.png"))
            files = scratch_retention.manifest(directory, paths, self.max_bytes)
            if any(
                (
                    files[spec["filename"]]["size_bytes"],
                    files[spec["filename"]]["sha256"],
                )
                != (spec["size_bytes"], spec["sha256"])
                for spec in receipt["objects"].values()
                if spec["filename"] in files
            ):
                raise IntegrityError("r2_asset_scratch_hash_mismatch")
            if backfill and backfill["source_name"] in files:
                original = receipt["asset"]["original"]
                if any(
                    files[backfill["source_name"]][k] != original[k]
                    for k in ("size_bytes", "sha256")
                ):
                    raise IntegrityError("r2_asset_scratch_hash_mismatch")
            if {path.name for path in directory.iterdir()} != set(files):
                raise IntegrityError("r2_asset_scratch_unexpected_file")
            stage_bytes = sum(spec["size_bytes"] for spec in files.values())
            if receipt["reserved"] != remote_bytes + stage_bytes:
                raise IntegrityError("r2_asset_scratch_accounting_mismatch")
            cleanup = receipt["scratch_cleanup"] = dict(
                phase="pending", files=files, bytes=stage_bytes
            )
            save = self.journal.save_renditions if renditions else self.journal.save
            save(receipt)  # Exact deletion intent before filesystem IO.
        if receipt["reserved"] != remote_bytes + cleanup["bytes"]:
            raise IntegrityError("r2_asset_scratch_accounting_mismatch")
        scratch_retention.remove_manifest(directory, cleanup["files"], self.max_bytes)
        receipt["reserved"] = remote_bytes
        cleanup.update(phase="complete", completed_at=time.time())
        save = self.journal.save_renditions if renditions else self.journal.save
        save(receipt, byte_delta=-cleanup["bytes"])
        if directory.exists():
            directory.rmdir()

    def upload(
        self,
        owner,
        project_id,
        source,
        filename,
        *,
        client_asset_id=None,
        parent_id=None,
        selection=None,
    ):
        ident = uuid.uuid4().hex
        with self._operation(ident):
            return self._upload(
                ident,
                owner,
                project_id,
                source,
                filename,
                client_asset_id=client_asset_id,
                parent_id=parent_id,
                selection=selection,
            )

    @contextmanager
    def _operation(self, ident):
        with asset_operation_lock(self.operation_dir, ident) as acquired:
            if not acquired:
                raise AssetConflict("素材操作仍在运行；不会接管或释放其收据")
            yield

    def _upload(
        self,
        ident,
        owner,
        project_id,
        source,
        filename,
        *,
        client_asset_id=None,
        parent_id=None,
        selection=None,
    ):
        _part(owner)
        if client_asset_id is not None:
            validate_client_asset_id(client_asset_id)
        display_name = Path(str(filename).replace("\\", "/")).name[:255]
        suffix = Path(display_name).suffix.lower()
        if suffix not in media.EXTENSIONS:
            raise media.MediaError("仅支持PNG/JPG/WEBP、MP4/MOV、WAV/MP3/FLAC")
        asset = {
            "id": ident,
            "asset_id": ident,
            "client_asset_id": client_asset_id,
            "project_id": project_id,
            "file_name": display_name,
            "kind": media.EXTENSIONS[suffix],
            "mime": media.MIMES[suffix],
            "content_type": media.MIMES[suffix],
            "created_at": time.time(),
            "parent_id": parent_id,
            "selection": selection,
            "status": "validating",
        }
        receipt = self.journal.create(owner, asset)
        receipt["suffix"] = suffix
        receipt["storage_binding"] = self.storage_binding
        if isinstance(self.store, S3ObjectStore) and self.store.config.provider == "r2":
            receipt["scratch_policy"] = scratch_retention.POLICY
        try:
            # A kill during receiving must still identify the exact original
            # storage/staging contract; an old unbound receipt is never guessed.
            self.journal.save(receipt)
            directory = self._stage(receipt)
            directory.mkdir(mode=0o700)
            with (directory / "receiving").open("xb") as target:
                count, checksum = _copy(source, target, self.max_bytes, None)
                target.flush()
                os.fsync(target.fileno())
            if count == 0:
                raise media.MediaError("素材为空")
            (directory / "receiving").rename(directory / ("source" + suffix))
            _sync_directory(directory)
            receipt.update(size_bytes=count, sha256=checksum)
            fingerprint = dict(
                sha256=checksum,
                size_bytes=count,
                mime=asset["mime"],
                parent_id=parent_id,
                selection=selection,
            )
            receipt["fingerprint"] = hashlib.sha256(
                json.dumps(fingerprint, sort_keys=True).encode()
            ).hexdigest()
            legacy = self.journal.legacy_match(receipt, client_asset_id)
            existing = (
                {"asset": legacy}
                if legacy
                else self.journal.bind(receipt, client_asset_id)
            )
            if existing:
                # Only this newly received exact duplicate is removed, after its
                # fingerprint matches the prior receipt. No stored object is removed.
                self._file(receipt, "source" + suffix).unlink()
                directory.rmdir()
                receipt["asset"]["status"] = "duplicate"
                self.journal.save(receipt)
                self._release(receipt)
                return self.public(existing["asset"])
            return self._run_locked(receipt)
        except Exception:
            if receipt["busy"]:
                receipt["asset"].update(
                    status="failed", error="素材接收未完成；已保留收到的文件"
                )
                self.journal.save(receipt)
                self._release(receipt)
            raise

    def _prepare(self, receipt):
        if receipt.get("prepared"):
            return
        source = self._file(receipt, "source" + receipt["suffix"])
        size, checksum = self._digest(source)
        if size != receipt["size_bytes"] or checksum != receipt["sha256"]:
            raise IntegrityError("保留的原始文件校验失败")
        info = media.inspect(source, receipt["asset"]["kind"])
        normalized, info = media.normalize(
            source, info, self._stage(receipt), max_output_bytes=self.max_bytes
        )
        specs = {
            "original": dict(
                filename=source.name,
                size_bytes=size,
                sha256=checksum,
                content_type=receipt["asset"]["mime"],
                phase="planned",
            )
        }
        if normalized:
            path = self._file(receipt, normalized.name)
            model_size, model_hash = self._digest(path)
            if not model_size:
                raise media.MediaError("规范化素材为空，未上传模型副本")
            with path.open("r+b") as stream:
                os.fsync(stream.fileno())
            specs["model"] = dict(
                filename=path.name,
                size_bytes=model_size,
                sha256=model_hash,
                content_type=media.MIMES[path.suffix],
                phase="planned",
            )
        if receipt["asset"]["kind"] in {"image", "video"}:
            specs.update(self._prepare_renditions(receipt, source, checksum))
        # Existing reservations normally cover these small copies. A worst
        # case full-size original+model must reserve the extra exact bytes
        # before any PUT; a quota rejection retains the same original receipt.
        required = 2 * sum(spec["size_bytes"] for spec in specs.values())
        self._reserve_to(receipt, required)
        _sync_directory(self._stage(receipt))
        info.update(bytes=size, sha256=checksum, mime=receipt["asset"]["mime"])
        if receipt.get("frame_extraction"):
            info["artifact_frame"] = dict(receipt["asset"]["selection"])
        receipt.update(objects=specs, prepared=True)
        receipt["asset"]["metadata"] = info
        self.journal.save(receipt)

    def _prepare_renditions(self, receipt, source, source_hash, *, renditions=False):
        for role in RENDITIONS:
            path = self._stage(receipt) / f"{role}-v1.webp"
            if path.exists():
                self._file(
                    receipt, path.name
                )  # Reject links before replacing an unpublished copy.
        if receipt["asset"]["kind"] == "video":
            frame = self._stage(receipt) / "poster-frame.png"
            if frame.exists() or frame.is_symlink():
                self._file(receipt, frame.name)
            self._reserve_to(
                receipt,
                max(
                    receipt["reserved"],
                    self._stage_bytes(receipt)
                    + min(POSTER_FRAME_BYTES, self.max_bytes)
                    + 2 * sum(cap for _, cap in RENDITIONS.values()),
                ),
                renditions=renditions,
            )
            specs = create_video_renditions(
                source, self._stage(receipt), max_bytes=self.max_bytes
            )
            self._file(
                receipt, frame.name
            ).unlink()  # Only this unpublished derived scratch.
        else:
            specs = create_image_renditions(
                source, self._stage(receipt), max_bytes=self.max_bytes
            )
        receipt["asset"]["rendition_metadata"] = dict(
            version=1,
            source_sha256=source_hash,
            **{
                role: {key: spec[key] for key in ("width", "height")}
                for role, spec in specs.items()
            },
        )
        return specs

    def _verify_stored(self, spec):
        info = self.store.stat(spec["key"])
        if (
            info.size_bytes != spec["size_bytes"]
            or info.content_type != spec["content_type"]
        ):
            raise IntegrityError("存储对象与素材上传记录不符")
        with self.store.open(spec["key"]) as source:
            size, checksum = _copy(source, _Sink(), spec["size_bytes"], spec["sha256"])
        if size != spec["size_bytes"]:
            raise IntegrityError("存储对象不完整")
        return ObjectInfo(
            info.provider,
            info.key,
            size,
            checksum,
            info.content_type,
            info.etag,
            info.version_id,
        )

    def _write_object(self, receipt, role, *, interrupted=False, renditions=False):
        if renditions and role not in RENDITIONS:
            raise AssetConflict("rendition_original_identity_changed")
        save = self.journal.save_renditions if renditions else self.journal.save
        spec = receipt["objects"][role]
        if spec["phase"] == "verified":
            self._verify_stored(spec)
            return
        path = self._file(receipt, spec["filename"])
        size, checksum = self._digest(path)
        if size != spec["size_bytes"] or checksum != spec["sha256"]:
            raise IntegrityError("素材暂存副本已变化，不会提交不同内容")
        if self.multipart and size > self.store.config.max_single_put_bytes:
            spec["phase"] = "multipart"
            save(receipt)
            try:
                state = self.multipart.begin(
                    receipt["owner"],
                    receipt["id"],
                    receipt["id"] + "-" + role,
                    size_bytes=size,
                    sha256=checksum,
                    filename=spec["filename"],
                    content_type=spec["content_type"],
                )
                spec.update(
                    session_id=state["id"],
                    key=self.multipart.journal.get(receipt["owner"], state["id"])[
                        "key"
                    ],
                )
                save(receipt)
                state = self.multipart.reconcile(
                    receipt["owner"], state["id"], interrupted=interrupted
                )
                if state["status"] == "active":
                    with path.open("rb") as source:
                        for number in range(1, state["part_count"] + 1):
                            if number in state["completed_parts"]:
                                continue
                            source.seek((number - 1) * state["part_size"])
                            remaining = min(state["part_size"], size - source.tell())
                            self.multipart.upload_part(
                                receipt["owner"],
                                state["id"],
                                number,
                                _PartReader(source, remaining),
                            )
                result = self.multipart.complete(receipt["owner"], state["id"])
            except MultipartOutcomeUnknown as error:
                spec.update(session_id=error.session_id, key=error.key)
                save(receipt)
                raise
        else:
            if spec["phase"] == "planned":
                spec.update(
                    key=make_object_key(
                        receipt["owner"], receipt["id"], spec["filename"]
                    ),
                    phase="putting",
                )
                save(receipt)
                try:
                    with path.open("rb") as source:
                        self.store.put(
                            spec["key"],
                            source,
                            content_type=spec["content_type"],
                            max_bytes=self.max_bytes,
                            expected_sha256=checksum,
                        )
                except ObjectAlreadyExists:
                    pass
                except Exception:
                    spec["phase"] = "put_unknown"
                    save(receipt)
                    raise StorageWriteUncertain(spec["key"]) from None
                spec["phase"] = "verifying"
                save(receipt)
            # Unknown outcomes only read back the existing key. Never new-key retry.
            result = self._verify_stored(spec)
        receipt["asset"][role] = asdict(result)
        spec["phase"] = "verified"
        save(receipt)

    def _run(self, receipt, *, interrupted=False):
        with self._operation(receipt["id"]):
            return self._run_locked(receipt, interrupted=interrupted)

    def _run_locked(self, receipt, *, interrupted=False):
        try:
            receipt["asset"].update(status="validating")
            receipt["asset"].pop("error", None)
            self.journal.save(receipt)
            if not receipt.get("prepared"):
                with media.PROCESSING_ADMISSION.acquire(
                    "video"
                    if receipt.get("frame_extraction")
                    else receipt["asset"]["kind"]
                ):
                    if receipt.get("derivation"):
                        self._prepare_derivation(receipt)
                    if receipt.get("frame_extraction"):
                        self._prepare_artifact_frame(receipt)
                    self._prepare(receipt)
            for role in ASSET_OBJECT_ROLES:
                if role in receipt["objects"]:
                    self._write_object(receipt, role, interrupted=interrupted)
            receipt["asset"]["status"] = "ready"
            self.journal.save(receipt)
        except Exception as error:
            uncertain = any(
                s["phase"] in {"putting", "put_unknown", "verifying", "multipart"}
                for s in receipt["objects"].values()
            )
            if uncertain:
                detail = "素材保存待核对，已保留原件，请恢复同一素材"
            elif isinstance(error, media.MediaBusy):
                detail = "素材处理繁忙；原件已保留，请恢复同一素材"
            elif isinstance(error, media.MediaError):
                # Our media layer uses bounded static diagnostics, never raw
                # decoder output. Keep the actionable reason after a refresh.
                detail = str(error)[:240]
                if "原素材" not in detail and "原件" not in detail:
                    detail += "；已保留原件"
            else:
                detail = "素材校验未完成，已保留原件"
            receipt["asset"].update(
                status="storage_unknown" if uncertain else "failed", error=detail
            )
            self.journal.save(receipt)
            self._release(receipt)
            raise
        self._release(receipt)
        return self.public(receipt["asset"])

    def resume(self, owner, asset_id):
        return self.reconcile(owner, asset_id)

    def ensure_image_renditions(
        self, owner, asset_id, *, expected_version=None, interrupted=False
    ):
        """Compatibility entry for the original image-only backfill command."""
        return self.ensure_renditions(
            owner, asset_id, expected_version=expected_version, interrupted=interrupted
        )

    def ensure_renditions(
        self,
        owner,
        asset_id,
        *,
        expected_version=None,
        interrupted=False,
        original_copy_receipts=None,
        original_local_root=None,
        original_copy_provenance=None,
    ):
        """Explicit offline backfill, on the original receipt and quota authority.

        Never called by GET. A legacy asset without an original bound upload
        receipt is reported as unsupported rather than adopted or re-accounted.
        Interrupted writers require the same explicit stopped-writer assertion
        and receipt CAS used by upload recovery; the local lock alone is not it.
        """
        with self._operation(asset_id):
            asset = self.get(owner, asset_id)
            if (
                asset.get("kind") not in {"image", "video"}
                or asset.get("status") != "ready"
            ):
                raise AssetConflict("rendition_requires_ready_media")
            receipt = self.journal.get(owner, asset_id)
            if expected_version is not None and (
                type(expected_version) is not int
                or receipt["version"] != expected_version
            ):
                raise AssetConflict("rendition_receipt_version_changed")
            if interrupted and expected_version is None:
                raise AssetConflict("rendition_interrupted_requires_receipt_version")
            if (
                not receipt.get("prepared")
                or not receipt.get("accepted_input")
                or receipt["asset"] != asset
                or receipt.get("suffix") not in media.EXTENSIONS
            ):
                raise AssetConflict("rendition_original_receipt_unconfirmed")
            if receipt["busy"] and not interrupted:
                raise AssetConflict("rendition_writer_still_active")
            copied_source = None
            if (
                receipt.get("storage_binding") != self.storage_binding
                or original_copy_receipts is not None
            ):
                if original_copy_receipts is None:
                    raise AssetConflict("rendition_original_receipt_unconfirmed")
                from .storage_migration import verify_rendition_sources

                copied_source = verify_rendition_sources(
                    original_copy_receipts,
                    self.store,
                    receipt,
                    logical_root=original_local_root,
                    provenance_file=original_copy_provenance,
                )
                backfill = receipt.get("rendition_backfill")
                if (
                    backfill is not None
                    and (
                        backfill.get("target_binding") != self.storage_binding
                        or backfill.get("copy_proof") != copied_source
                    )
                    or backfill is None
                    and any(role in receipt["objects"] for role in RENDITIONS)
                ):
                    raise AssetConflict("rendition_copy_target_changed")
            elif (
                original_local_root is not None or original_copy_provenance is not None
            ):
                raise AssetConflict("rendition_copy_proof_required")
            if public_renditions(asset).get("rendition_ready") and not receipt["busy"]:
                self._cleanup_after_release(receipt, renditions=True)
                return self.public(asset)
            # Finish an already accepted upload cleanup before changing its
            # exact manifest. Failure leaves it intact for explicit recovery.
            self._cleanup_after_release(receipt, renditions=True)
            cleanup = receipt.get("scratch_cleanup")
            if cleanup and cleanup.get("phase") != "complete":
                raise AssetConflict("rendition_scratch_cleanup_pending")
            if copied_source is not None and receipt.get("rendition_backfill") is None:
                receipt["rendition_backfill"] = dict(
                    phase="planned",
                    source_name="rendition-source" + receipt["suffix"],
                    source=dict(asset["original"]),
                    target_binding=self.storage_binding,
                    copy_proof=copied_source,
                )
            self.journal.claim(receipt, interrupted=interrupted, renditions=True)
            try:
                backfill = receipt.get("rendition_backfill")
                if backfill is None:
                    original = asset["original"]
                    spec = receipt["objects"].get("original", {})
                    if spec.get("phase") != "verified" or any(
                        spec.get(k) != original.get(k)
                        for k in ("key", "size_bytes", "sha256", "content_type")
                    ):
                        raise AssetConflict("rendition_original_receipt_unconfirmed")
                    backfill = receipt["rendition_backfill"] = dict(
                        phase="planned",
                        source_name="rendition-source" + receipt["suffix"],
                        source=dict(original),
                    )
                    receipt["asset"]["rendition_status"] = "pending"
                    self.journal.save_renditions(
                        receipt
                    )  # Frozen source before borrowing/encoding bytes.
                elif copied_source is not None:
                    receipt["asset"]["rendition_status"] = "pending"
                    self.journal.save_renditions(receipt)
                if backfill["source"] != receipt["asset"]["original"]:
                    raise AssetConflict("rendition_original_identity_changed")
                directory = self._stage(receipt)
                directory.mkdir(mode=0o700, exist_ok=True)
                if backfill["phase"] == "planned":
                    original = backfill["source"]
                    source_path = directory / backfill["source_name"]
                    # Repeat this on every preparation retry: a previous
                    # ordinary failure may have released its unused headroom.
                    remote = sum(
                        receipt['asset'].get(role, {}).get('size_bytes', 0)
                        for role in ASSET_OBJECT_ROLES
                    )
                    future_copy = 0 if source_path.exists() else original['size_bytes']
                    headroom = 2 * sum(
                        min(cap, self.max_bytes) for _, cap in RENDITIONS.values()
                    )
                    self._reserve_to(
                        receipt,
                        remote + self._stage_bytes(receipt) + future_copy + headroom,
                        renditions=True,
                    )
                    if source_path.exists():
                        if self._digest(self._file(receipt, source_path.name)) != (
                            original["size_bytes"],
                            original["sha256"],
                        ):
                            raise IntegrityError("rendition_original_copy_changed")
                    else:
                        receiving = directory / "rendition-receiving"
                        if receiving.exists():
                            self._file(
                                receipt, receiving.name
                            ).unlink()  # Only this unfinished borrowed copy.
                        actual = self.store.stat(original["key"])
                        if (actual.size_bytes, actual.content_type) != (
                            original["size_bytes"],
                            original["content_type"],
                        ):
                            raise IntegrityError("rendition_original_object_changed")
                        with (
                            self.store.open(original["key"]) as source,
                            receiving.open("xb") as target,
                        ):
                            size, _ = _copy(
                                source, target, self.max_bytes, original["sha256"]
                            )
                            target.flush()
                            os.fsync(target.fileno())
                        if size != original["size_bytes"]:
                            raise IntegrityError("rendition_original_copy_incomplete")
                        receiving.rename(source_path)
                    with media.PROCESSING_ADMISSION.acquire(asset["kind"]):
                        media.inspect(source_path, asset["kind"])
                        specs = self._prepare_renditions(
                            receipt, source_path, original["sha256"], renditions=True
                        )
                    # Do not replace a frozen unknown PUT. A prepared backfill
                    # only resumes its original specs and exact object keys.
                    if any(role in receipt["objects"] for role in RENDITIONS):
                        raise AssetConflict(
                            "rendition_existing_objects_require_recovery"
                        )
                    receipt["objects"].update(specs)
                    backfill["phase"] = "prepared"
                    receipt.pop("scratch_cleanup", None)
                    _sync_directory(directory)
                    self.journal.save_renditions(receipt)
                # Prepared retries also reserve every still-unwritten frozen
                # role before PUT; no new key/body is generated to fit quota.
                self._reserve_to(
                    receipt,
                    self._stage_bytes(receipt)
                    + sum(spec['size_bytes'] for spec in receipt['objects'].values()),
                    renditions=True,
                )
                for role in RENDITIONS:
                    self._write_object(
                        receipt, role, interrupted=interrupted, renditions=True
                    )
                backfill["phase"] = "complete"
                receipt["asset"].pop("rendition_status", None)
                self.journal.save_renditions(receipt)
            except Exception:
                # Process interruption keeps the busy receipt for explicit
                # stopped-writer recovery; ordinary errors retain proven bytes.
                self._release(receipt, renditions=True)
                raise
            self._release(receipt, renditions=True)
            return self.public(receipt["asset"])

    def reconcile(self, owner, asset_id, *, interrupted=False, expected_version=None):
        # The local lock is taken before receipt claim and held through release.
        # An operator assertion never overrides a still-running local operation.
        asset = self.get(owner, asset_id)
        if asset["status"] == "ready" and not interrupted and expected_version is None:
            with self._operation(asset_id):
                self._cleanup_committed_asset(owner, asset_id)
            return self.public(asset)
        with self._operation(asset_id):
            return self._reconcile_locked(
                owner,
                asset_id,
                interrupted=interrupted,
                expected_version=expected_version,
            )

    def _reconcile_locked(
        self, owner, asset_id, *, interrupted=False, expected_version=None
    ):
        asset = self.get(owner, asset_id)
        if asset["status"] == "ready" and not interrupted and expected_version is None:
            self._cleanup_committed_asset(owner, asset_id)
            return self.public(asset)
        receipt = self.journal.get(owner, asset_id)
        if expected_version is not None and (
            type(expected_version) is not int
            or expected_version < 0
            or receipt["version"] != expected_version
        ):
            raise AssetConflict("素材收据版本已变化；请重新只读核对")
        if receipt.get("storage_binding") != self.storage_binding:
            raise AssetConflict("素材上传属于另一存储位置或身份，未自动切换供应商")
        if not receipt.get("accepted_input") or not (
            receipt.get("size_bytes")
            or receipt.get("derivation")
            or receipt.get("frame_extraction")
        ):
            raise AssetConflict("文件未完整接收，需要重新选择文件上传")
        if asset["status"] == "ready":
            if receipt["busy"]:
                if not interrupted:
                    return self.public(asset)
                if not receipt.get("prepared") or "original" not in receipt["objects"]:
                    raise AssetConflict("就绪素材收据证据不完整；未释放预留")
                for role, spec in receipt["objects"].items():
                    actual = asset.get(role, {})
                    if (
                        role not in ASSET_OBJECT_ROLES
                        or spec.get("phase") != "verified"
                        or any(
                            actual.get(k) != spec.get(k)
                            for k in ("key", "size_bytes", "sha256", "content_type")
                        )
                    ):
                        raise AssetConflict(
                            "就绪素材与已验证对象证据不一致；未释放预留"
                        )
                    self._verify_stored(spec)
                # Ready was saved before the independent quota-release commit.
                # Reconcile bytes and release once; never generate or write keys.
                self._release(receipt)
            else:
                self._cleanup_after_release(receipt)
            return self.public(asset)
        self.journal.claim(receipt, interrupted=interrupted)
        return self._run_locked(receipt, interrupted=interrupted)

    def settle_incomplete(
        self, owner, asset_id, *, expected_version, writers_stopped=False
    ):
        """Operator-only: account for retained partial bytes; never make ready.

        Shared local lock blocks actual live operations. An explicit assertion
        remains necessary for old code/other hosts not sharing this local lock.
        """
        if writers_stopped is not True:
            raise AssetConflict("必须先确认所有原写入者已停止")
        self.get(owner, asset_id)
        with self._operation(asset_id):
            receipt = self.journal.get(owner, asset_id)
            if (
                type(expected_version) is not int
                or expected_version < 0
                or receipt["version"] != expected_version
            ):
                raise AssetConflict("素材收据版本已变化；请重新只读核对")
            if (
                receipt.get("storage_binding") != self.storage_binding
                or receipt.get("suffix") not in media.EXTENSIONS
            ):
                raise AssetConflict("不完整旧收据没有匹配的存储身份；未猜测或释放预留")
            if (
                receipt.get("accepted_input")
                or receipt.get("prepared")
                or receipt.get("derivation")
                or receipt["objects"]
                or receipt["asset"]["status"] == "ready"
            ):
                raise AssetConflict(
                    "此收据不是单纯未完整接收；请核对原对象或使用完整素材恢复"
                )
            if receipt["busy"]:
                receipt["asset"].update(
                    status="failed",
                    error="原文件接收未完成；旧写入者已停止，已保留收到的字节，请重新选择文件上传",
                )
                self.journal.save(receipt)
                self._release(receipt)
            return self.public(receipt["asset"])

    def get(self, owner, asset_id, project_id=None):
        clauses = [
            asset_table.c.id == asset_id,
            asset_table.c.tenant == self.tenant,
            asset_table.c.owner == owner,
        ]
        if project_id is not None:
            clauses.append(asset_table.c.project_id == project_id)
        with self.engine.connect() as conn:
            value = conn.execute(
                select(asset_table.c.record).where(*clauses)
            ).scalar_one_or_none()
            if value is None:
                raise AssetNotFound("素材不存在")
            record = json.loads(value)
            self.journal._available(conn, owner, record, acquire_gate=False)
            return record

    def list(self, owner, project_id):
        with self.engine.connect() as conn:
            rows = conn.execute(
                select(asset_table.c.record)
                .where(
                    asset_table.c.tenant == self.tenant,
                    asset_table.c.owner == owner,
                    asset_table.c.project_id == project_id,
                    asset_table.c.status != "duplicate",
                )
                .order_by(asset_table.c.created.desc())
                .limit(500)
            ).scalars()
            result = []
            for raw in rows:
                record = json.loads(raw)
                keys = [
                    record[role]["key"]
                    for role in ASSET_OBJECT_ROLES
                    if record.get(role, {}).get("key")
                ]
                from .universal_trash import active_target

                if active_target(
                    conn,
                    self.tenant,
                    owner,
                    {"kind": "asset", "project_id": project_id, "id": record["id"]},
                ):
                    continue
                if content_is_deleted(
                    conn, self.tenant, owner, asset_id=record["id"]
                ) or any(
                    content_is_deleted(conn, self.tenant, owner, object_key=key)
                    for key in keys
                ):
                    continue
                result.append(self.public(record))
            return result

    def finalize_purge(self, connection, owner, *, asset_ids, artifact_specs, now):
        """Release proven-absent originals in the lifecycle final transaction.

        The caller owns the purge action/tombstones and marks accounting_released
        in this SAME transaction. No storage IO, schema initialization, nested
        connection, scratch deletion, financial settlement, or receipt deletion.
        """
        owner_reference_gate(connection, self.tenant, owner)
        if len(set(asset_ids)) != len(asset_ids):
            raise AssetConflict("purge_asset_identity_duplicate")
        physical = {}

        def add(key, size, sha):
            ident, size, sha = artifact_identity(
                dict(object_key=key, size_bytes=size, sha256=sha, validated=True), owner
            )
            if key in physical and physical[key] != (size, sha):
                raise AssetConflict("purge_object_identity_conflict")
            physical[key] = (size, sha)
            return ident

        for asset_id in sorted(asset_ids):
            row = (
                connection.execute(
                    select(asset_table)
                    .where(
                        asset_table.c.id == asset_id,
                        asset_table.c.tenant == self.tenant,
                        asset_table.c.owner == owner,
                    )
                    .with_for_update()
                )
                .mappings()
                .first()
            )
            upload = (
                connection.execute(
                    select(receipts)
                    .where(
                        receipts.c.id == asset_id,
                        receipts.c.tenant == self.tenant,
                        receipts.c.owner == owner,
                    )
                    .with_for_update()
                )
                .mappings()
                .first()
            )
            if row is None or upload is None:
                raise AssetConflict("purge_original_asset_unconfirmed")
            asset, receipt = json.loads(row["record"]), json.loads(upload["record"])
            if (
                row["status"] != "ready"
                or receipt.get("busy") is not False
                or receipt.get("asset") != asset
                or receipt.get("storage_binding") != self.storage_binding
                or not receipt.get("prepared")
                or not receipt.get("objects")
                or not content_is_deleted(
                    connection, self.tenant, owner, asset_id=asset_id
                )
            ):
                raise AssetConflict("purge_original_upload_unconfirmed")
            remote = 0
            for role, spec in receipt["objects"].items():
                original = asset.get(role, {})
                if (
                    role not in ASSET_OBJECT_ROLES
                    or spec.get("phase") != "verified"
                    or any(
                        original.get(k) != spec.get(k)
                        for k in ("key", "size_bytes", "sha256", "content_type")
                    )
                    or not content_is_deleted(
                        connection,
                        self.tenant,
                        owner,
                        object_key=spec["key"],
                        storage_binding=self.storage_binding,
                    )
                ):
                    raise AssetConflict("purge_original_upload_unconfirmed")
                add(spec["key"], spec["size_bytes"], spec["sha256"])
                remote += spec["size_bytes"]
            if type(receipt.get("reserved")) is not int or receipt["reserved"] < remote:
                raise AssetConflict("purge_upload_accounting_unconfirmed")
        for spec in artifact_specs:
            if spec.get(
                "storage_binding"
            ) != self.artifact_storage_binding or not content_is_deleted(
                connection,
                self.tenant,
                owner,
                object_key=spec.get("object_key"),
                storage_binding=self.artifact_storage_binding,
            ):
                raise AssetConflict("purge_artifact_binding_unconfirmed")
            ident = add(
                spec.get("object_key"), spec.get("size_bytes"), spec.get("sha256")
            )
            for scope, _, _ in self.journal._scopes(owner):
                marker = (
                    connection.execute(
                        select(artifact_accounting)
                        .where(
                            artifact_accounting.c.scope == scope,
                            artifact_accounting.c.object_id == ident,
                        )
                        .with_for_update()
                    )
                    .mappings()
                    .first()
                )
                if marker is None or (marker["size_bytes"], marker["sha256"]) != (
                    spec["size_bytes"],
                    spec["sha256"],
                ):
                    raise AssetConflict("purge_artifact_accounting_unconfirmed")
        released = sum(size for size, _ in physical.values())
        if released:
            self.journal._change(connection, owner, -released, 0)
        for asset_id in sorted(asset_ids):
            connection.execute(
                delete(asset_table).where(
                    asset_table.c.id == asset_id,
                    asset_table.c.tenant == self.tenant,
                    asset_table.c.owner == owner,
                )
            )
        return {"removed_asset_ids": sorted(asset_ids), "released_bytes": released}

    def model_snapshot(self, owner, project_id, asset_id):
        record = self.get(owner, asset_id, project_id)
        if (
            record["status"] != "ready"
            or not record.get("metadata", {}).get("model_ready")
            or "model" not in record
        ):
            raise media.MediaError("参考素材未就绪；长视频/音频请先选择2–15秒片段")
        return {
            "asset_id": asset_id,
            "metadata": record["metadata"],
            "model": record["model"],
            "original": record["original"],
            "parent_id": record.get("parent_id"),
            "selection": record.get("selection"),
        }

    def derive(self, owner, asset_id, start, end):
        ident = uuid.uuid4().hex
        with self._operation(ident):
            return self._derive(ident, owner, asset_id, start, end)

    def artifact_frame(
        self, owner, project_id, actor_id, artifact_id, job_id, metadata, body, key
    ):
        """One existing image-asset receipt per explicit extraction decision.

        The HTTP service has already checked project/artifact ownership. Stable
        identity is bound before downloading/decoding, so response loss resumes
        the same receipt and uncertain PUTs only reconcile their original keys.
        """
        from .artifact_frames import fingerprint, selection, source_spec
        from .repository import identifier

        requested = selection(body)
        identifier(key)
        source = source_spec(metadata, owner, self.max_bytes)
        ident = fingerprint(
            ["artifact-frame-v1", self.tenant, owner, project_id, actor_id, key]
        )[:32]
        request_hash = fingerprint([artifact_id, job_id, source, requested])
        with self._operation(ident):
            try:
                current = self.get(owner, ident, project_id)
            except AssetNotFound:
                current = None
            if current is not None:
                if current.get("frame_request_hash") != request_hash:
                    raise AssetConflict("frame_idempotency_conflict")
                receipt = self.journal.get(owner, ident)
                if receipt.get("storage_binding") != self.storage_binding:
                    raise AssetConflict("素材收据没有匹配的存储身份；请核对原存储配置")
                if current["status"] == "ready":
                    self._cleanup_committed_asset(owner, ident)
                    return self.public(current)
                # A stopped API can leave a live receipt. Never infer takeover
                # from an expired timer; the existing admin recovery fences it.
                if receipt["busy"]:
                    raise AssetConflict(
                        "截帧仍在处理或等待原操作恢复；请核对同一素材记录"
                    )
                self.journal.claim(receipt)
            else:
                provenance = {
                    "kind": "artifact_frame",
                    "artifact_id": artifact_id,
                    "job_id": job_id,
                    "source_sha256": source["sha256"],
                    **requested,
                }
                asset = {
                    "id": ident,
                    "asset_id": ident,
                    "client_asset_id": "frame:" + ident,
                    "project_id": project_id,
                    "file_name": f"frame-{artifact_id[:8]}-{requested['position']}.png",
                    "kind": "image",
                    "mime": "image/png",
                    "content_type": "image/png",
                    "created_at": time.time(),
                    "parent_id": None,
                    "selection": provenance,
                    "frame_request_hash": request_hash,
                    "status": "validating",
                }
                receipt = self.journal.create(owner, asset)
            receipt.update(
                suffix=".png", storage_binding=self.storage_binding, accepted_input=True
            )
            receipt.setdefault(
                "frame_extraction", {"original": source, "phase": "planned"}
            )
            if (
                isinstance(self.store, S3ObjectStore)
                and self.store.config.provider == "r2"
            ):
                receipt["scratch_policy"] = scratch_retention.POLICY
            self.journal.save(receipt)
            return self._run_locked(receipt)

    def _prepare_artifact_frame(self, receipt):
        from .artifact_frames import extract

        spec = receipt["frame_extraction"]
        directory = self._stage(receipt)
        directory.mkdir(mode=0o700, exist_ok=True)
        if spec["phase"] != "ready":
            source_path = directory / "artifact-input.mp4"
            original = spec["original"]
            if source_path.exists():
                source_path = self._file(receipt, source_path.name)
                count, checksum = self._digest(source_path)
                if (count, checksum) != (original["size_bytes"], original["sha256"]):
                    raise IntegrityError("截帧源文件副本校验失败；原视频保持不变")
            else:
                receiving = directory / "receiving"
                if receiving.exists():
                    self._file(receipt, "receiving").unlink()
                actual = self.store.stat(original["key"])
                if (actual.size_bytes, actual.content_type) != (
                    original["size_bytes"],
                    original["content_type"],
                ):
                    raise IntegrityError("截帧源对象与核验记录不符；原视频保持不变")
                with (
                    self.store.open(original["key"]) as source,
                    receiving.open("xb") as target,
                ):
                    count, _ = _copy(source, target, self.max_bytes, original["sha256"])
                    target.flush()
                    os.fsync(target.fileno())
                if count != original["size_bytes"]:
                    raise IntegrityError("截帧源文件副本不完整")
                receiving.rename(source_path)
                _sync_directory(directory)
            if (directory / "extracted.png").exists():
                self._file(receipt, "extracted.png")
            target, measured = extract(
                source_path,
                directory,
                receipt["asset"]["selection"],
                max_output_bytes=self.max_bytes,
            )
            target = self._file(receipt, target.name)
            count, checksum = self._digest(target)
            with target.open("r+b") as stream:
                os.fsync(stream.fileno())
            destination = directory / "source.png"
            if destination.exists():
                self._file(receipt, destination.name)
            target.replace(destination)
            _sync_directory(directory)
            receipt["asset"]["selection"].update(measured)
            receipt.update(size_bytes=count, sha256=checksum)
            spec["phase"] = "ready"
            self.journal.save(receipt)
        # Only the verified borrowed copy is removed, never the source artifact.
        input_path = directory / "artifact-input.mp4"
        if input_path.exists():
            self._file(receipt, input_path.name).unlink()
            _sync_directory(directory)

    def _derive(self, ident, owner, asset_id, start, end):
        parent = self.get(owner, asset_id)
        if parent["status"] != "ready":
            raise media.MediaError("原素材尚未就绪")
        info = parent["metadata"]
        if info["kind"] not in {"video", "audio"}:
            raise media.MediaError("当前仅视频/音频支持时间选段")
        if (
            type(start) not in (int, float)
            or type(end) not in (int, float)
            or not math.isfinite(start)
            or not math.isfinite(end)
            or start < 0
            or end > info["source_duration"] + 0.01
            or not 2 <= end - start <= 15
        ):
            raise media.MediaError("选段须在原素材范围内，且长度为2–15秒")
        # Reserve the same durable capacity/concurrency slot as uploads before
        # downloading or invoking FFmpeg. The receipt also survives API restart.
        suffix = ".mp4" if info["kind"] == "video" else ".wav"
        asset = {
            "id": ident,
            "asset_id": ident,
            "client_asset_id": None,
            "project_id": parent["project_id"],
            "file_name": "trimmed" + suffix,
            "kind": info["kind"],
            "mime": media.MIMES[suffix],
            "content_type": media.MIMES[suffix],
            "created_at": time.time(),
            "parent_id": asset_id,
            "selection": {"start": start, "end": end},
            "status": "validating",
        }
        receipt = self.journal.create(owner, asset)
        receipt.update(
            suffix=suffix,
            storage_binding=self.storage_binding,
            accepted_input=True,
            derivation={
                "input_name": "derivation-input"
                + Path(parent["file_name"]).suffix.lower(),
                "original": dict(parent["original"]),
                "metadata": dict(info),
                "phase": "planned",
            },
        )
        if isinstance(self.store, S3ObjectStore) and self.store.config.provider == "r2":
            receipt["scratch_policy"] = scratch_retention.POLICY
        self.journal.save(receipt)
        return self._run_locked(receipt)

    def _prepare_derivation(self, receipt):
        spec = receipt["derivation"]
        directory = self._stage(receipt)
        directory.mkdir(mode=0o700, exist_ok=True)
        if spec["phase"] != "ready":
            source_path = directory / spec["input_name"]
            original = spec["original"]
            if source_path.exists():
                source_path = self._file(receipt, spec["input_name"])
                count, checksum = self._digest(source_path)
                if count != original["size_bytes"] or checksum != original["sha256"]:
                    raise IntegrityError("裁剪源文件副本校验失败；原始存储对象保持不变")
            else:
                # A partial receiving file is not treated as a complete source.
                # It is safe to retry this read of the existing immutable object.
                receiving = directory / "receiving"
                if receiving.exists():
                    self._file(receipt, "receiving").unlink()
                with (
                    self.store.open(original["key"]) as source,
                    receiving.open("xb") as target,
                ):
                    count, _ = _copy(source, target, self.max_bytes, original["sha256"])
                    target.flush()
                    os.fsync(target.fileno())
                if count != original["size_bytes"]:
                    raise IntegrityError("裁剪源文件副本不完整")
                receiving.rename(source_path)
                _sync_directory(directory)
            selected = receipt["asset"]["selection"]
            # All paths are private server-owned staging entries. A retry may
            # overwrite only its unfinished derived output, never an original.
            expected_name = "trimmed" + receipt["suffix"]
            if (directory / expected_name).exists():
                self._file(receipt, expected_name)
            target = media.derive(
                source_path,
                spec["metadata"],
                directory,
                selected["start"],
                selected["end"],
                max_output_bytes=self.max_bytes,
            )
            target = self._file(receipt, target.name)
            count, checksum = self._digest(target)
            if not count:
                raise media.MediaError("裁剪素材为空")
            with target.open("r+b") as stream:
                os.fsync(stream.fileno())
            destination = directory / ("source" + receipt["suffix"])
            if destination.exists():
                # A crash between publication and receipt persistence may leave
                # the previous exact derived file. Never replace a linked path.
                self._file(receipt, destination.name)
            target.replace(destination)
            _sync_directory(directory)
            receipt.update(size_bytes=count, sha256=checksum)
            spec["phase"] = "ready"
            self.journal.save(receipt)
        # The source remains in its verified object store; removing this borrowed
        # processing copy keeps the normal 4*max_bytes reservation sufficient.
        input_path = directory / spec["input_name"]
        if input_path.exists():
            self._file(receipt, spec["input_name"]).unlink()
            _sync_directory(directory)

    @staticmethod
    def public(record):
        fields = {
            "id",
            "asset_id",
            "client_asset_id",
            "project_id",
            "file_name",
            "kind",
            "mime",
            "content_type",
            "created_at",
            "parent_id",
            "selection",
            "status",
            "metadata",
            "error",
        }
        clean = {k: v for k, v in record.items() if k in fields}
        clean["content_url"] = f'/v1/assets/{record["id"]}/content'
        clean.update(public_renditions(record))
        return clean
