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

from sqlalchemy import Column, Float, MetaData, String, Table, Text, select

from . import media
from .storage import (IntegrityError, LocalObjectStore, ObjectAlreadyExists, ObjectInfo, S3ObjectStore,
    StorageWriteUncertain, UnsupportedStorageOperation, _check_ancestors, _copy,
    _no_links, _part, _sync_directory, make_object_key)
from .storage_asset_journal import AssetUploadJournal, AssetConflict, AssetQuotaExceeded, validate_client_asset_id
from .storage_multipart import MultipartJournal, MultipartOutcomeUnknown, MultipartUploadManager
from .storage_schema import create_storage_schema
from .asset_operation import asset_operation_lock
from . import scratch_retention

MIB = 1024 * 1024
metadata = MetaData()
asset_table = Table("platform_assets", metadata,
    Column("id", String(32), primary_key=True), Column("tenant", String(80), nullable=False),
    Column("owner", String(80), nullable=False), Column("project_id", String(160), nullable=False),
    Column("status", String(30), nullable=False), Column("created", Float, nullable=False),
    Column("record", Text, nullable=False))


class AssetNotFound(LookupError):
    pass


class _Sink:
    def write(self, value):
        return len(value)


class _PartReader:
    def __init__(self, source, size):
        self.source, self.remaining = source, size

    def read(self, size=-1):
        data = self.source.read(self.remaining if size < 0 else min(size, self.remaining))
        self.remaining -= len(data)
        return data


class AssetService:
    # Suggested application caps, not supplier limits. Reservations include
    # original/model plus durable staging until verified publication commits.
    # Local and pre-cutover receipts keep their existing copies untouched.
    def __init__(self, engine, store, data_dir, *, tenant="sixnine", max_bytes=512*MIB,
                 owner_quota_bytes=10*1024*MIB, tenant_quota_bytes=40*1024*MIB,
                 max_active_per_owner=2, max_active_total=4):
        self.engine, self.store, self.tenant, self.max_bytes = engine, store, _part(tenant), max_bytes
        if type(max_bytes) is not int or not 1 <= max_bytes <= 512*MIB:
            raise ValueError("素材单文件上限必须为1字节至512 MiB")
        if not store.capabilities.conditional_create:
            raise UnsupportedStorageOperation("素材服务需要条件创建；Hippius实验适配器尚未接入素材服务")
        if isinstance(store, S3ObjectStore):
            identity = {k: getattr(store.config, k) for k in
                        ("provider", "endpoint_url", "region", "bucket", "service", "profile")}
        elif isinstance(store, LocalObjectStore):
            identity = {"provider": "local", "root": str(store.root)}
        else:
            raise UnsupportedStorageOperation("素材存储适配器尚未提供可验证的持久身份绑定")
        self.storage_binding = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        root = Path(data_dir)
        if not root.is_absolute():
            raise ValueError("素材数据根必须为绝对路径")
        self.temp_dir, self.staging_dir = root / "processing", root / "asset-staging"
        self.operation_dir = root / "asset-operation-locks"
        for directory in (self.temp_dir, self.staging_dir, self.operation_dir):
            _check_ancestors(directory)
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        create_storage_schema(engine, metadata)
        self.journal = AssetUploadJournal(engine, asset_table, tenant=tenant, reservation_bytes=4*max_bytes,
            owner_quota_bytes=owner_quota_bytes, tenant_quota_bytes=tenant_quota_bytes,
            max_active_per_owner=max_active_per_owner, max_active_total=max_active_total)
        self.multipart = (MultipartUploadManager(store, MultipartJournal(engine, tenant), max_object_bytes=max_bytes)
                          if isinstance(store, S3ObjectStore) else None)

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
        names = {"source"+receipt["suffix"], "normalized.png", "normalized.wav", "normalized.mp4", "receiving"}
        if receipt.get("derivation"):
            names.update({receipt["derivation"]["input_name"], "trimmed.mp4", "trimmed.wav"})
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

    def _release(self, receipt):
        uncertain = {"putting", "put_unknown", "verifying", "multipart"}
        if any(s.get("phase") in uncertain for s in receipt["objects"].values()):
            retained = receipt["reserved"]
        else:
            retained = 0
            directory = self._stage(receipt)
            if directory.exists():
                for path in directory.iterdir():
                    info = _no_links(path)
                    if not path.is_file() or info.st_nlink != 1:
                        raise IntegrityError("素材暂存出现未预期文件")
                    retained += info.st_size
            retained += sum(receipt["asset"].get(role, {}).get("size_bytes", 0) for role in ("original", "model"))
        self.journal.release(receipt, retained)
        self._cleanup_after_release(receipt)

    def _cleanup_after_release(self, receipt):
        try:
            self._cleanup_ready(receipt)
        except Exception:
            # Cleanup is after acceptance: never report a ready upload as failed
            # or invite a duplicate submission because retained scratch remains.
            logging.getLogger(__name__).warning("r2_asset_scratch_cleanup_pending")

    def _cleanup_committed_asset(self, owner, asset_id):
        if not isinstance(self.store, S3ObjectStore) or self.store.config.provider != "r2":
            return
        try:
            receipt = self.journal.get(owner, asset_id)
        except AssetConflict:
            # Historical published assets may have no upload receipt at all.
            return
        self._cleanup_after_release(receipt)

    def _cleanup_ready(self, receipt):
        if (not isinstance(self.store, S3ObjectStore) or self.store.config.provider != "r2"
                or receipt.get("scratch_policy") != scratch_retention.POLICY
                or receipt.get("storage_binding") != self.storage_binding
                or receipt["busy"] or receipt["asset"]["status"] != "ready"):
            return
        cleanup = receipt.get("scratch_cleanup")
        if cleanup and cleanup["phase"] == "complete":
            return
        if not receipt.get("prepared") or "original" not in receipt["objects"]:
            raise IntegrityError("r2_asset_scratch_evidence_missing")
        for role, spec in receipt["objects"].items():
            actual = receipt["asset"].get(role, {})
            if (role not in {"original", "model"} or spec.get("phase") != "verified"
                    or any(actual.get(k) != spec.get(k) for k in ("key", "size_bytes", "sha256", "content_type"))):
                raise IntegrityError("r2_asset_scratch_evidence_mismatch")
            self._verify_stored(spec)
        directory = self._stage(receipt)
        remote_bytes = sum(spec["size_bytes"] for spec in receipt["objects"].values())
        if cleanup is None:
            paths = [self._file(receipt, spec["filename"]) for spec in receipt["objects"].values()]
            files = scratch_retention.manifest(directory, paths, self.max_bytes)
            if any((files[spec["filename"]]["size_bytes"], files[spec["filename"]]["sha256"])
                   != (spec["size_bytes"], spec["sha256"]) for spec in receipt["objects"].values()):
                raise IntegrityError("r2_asset_scratch_hash_mismatch")
            if {path.name for path in directory.iterdir()} != set(files):
                raise IntegrityError("r2_asset_scratch_unexpected_file")
            stage_bytes = sum(spec["size_bytes"] for spec in files.values())
            if receipt["reserved"] != remote_bytes + stage_bytes:
                raise IntegrityError("r2_asset_scratch_accounting_mismatch")
            cleanup = receipt["scratch_cleanup"] = dict(phase="pending", files=files, bytes=stage_bytes)
            self.journal.save(receipt)  # Exact deletion intent before filesystem IO.
        if receipt["reserved"] != remote_bytes + cleanup["bytes"]:
            raise IntegrityError("r2_asset_scratch_accounting_mismatch")
        scratch_retention.remove_manifest(directory, cleanup["files"], self.max_bytes)
        receipt["reserved"] = remote_bytes
        cleanup.update(phase="complete", completed_at=time.time())
        self.journal.save(receipt, byte_delta=-cleanup["bytes"])
        if directory.exists():
            directory.rmdir()

    def upload(self, owner, project_id, source, filename, *, client_asset_id=None, parent_id=None, selection=None):
        ident = uuid.uuid4().hex
        with self._operation(ident):
            return self._upload(ident, owner, project_id, source, filename,
                client_asset_id=client_asset_id, parent_id=parent_id, selection=selection)

    @contextmanager
    def _operation(self, ident):
        with asset_operation_lock(self.operation_dir, ident) as acquired:
            if not acquired:
                raise AssetConflict("素材操作仍在运行；不会接管或释放其收据")
            yield

    def _upload(self, ident, owner, project_id, source, filename, *, client_asset_id=None, parent_id=None, selection=None):
        _part(owner)
        if client_asset_id is not None:
            validate_client_asset_id(client_asset_id)
        display_name = Path(str(filename).replace("\\", "/")).name[:255]
        suffix = Path(display_name).suffix.lower()
        if suffix not in media.EXTENSIONS:
            raise media.MediaError("仅支持PNG/JPG/WEBP、MP4/MOV、WAV/MP3/FLAC")
        asset = {"id": ident, "asset_id": ident, "client_asset_id": client_asset_id,
            "project_id": project_id, "file_name": display_name, "kind": media.EXTENSIONS[suffix],
            "mime": media.MIMES[suffix], "content_type": media.MIMES[suffix], "created_at": time.time(),
            "parent_id": parent_id, "selection": selection, "status": "validating"}
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
            (directory / "receiving").rename(directory / ("source"+suffix))
            _sync_directory(directory)
            receipt.update(size_bytes=count, sha256=checksum)
            fingerprint = dict(sha256=checksum, size_bytes=count, mime=asset["mime"], parent_id=parent_id, selection=selection)
            receipt["fingerprint"] = hashlib.sha256(json.dumps(fingerprint, sort_keys=True).encode()).hexdigest()
            legacy = self.journal.legacy_match(receipt, client_asset_id)
            existing = {"asset": legacy} if legacy else self.journal.bind(receipt, client_asset_id)
            if existing:
                # Only this newly received exact duplicate is removed, after its
                # fingerprint matches the prior receipt. No stored object is removed.
                self._file(receipt, "source"+suffix).unlink()
                directory.rmdir()
                receipt["asset"]["status"] = "duplicate"
                self.journal.save(receipt)
                self._release(receipt)
                return self.public(existing["asset"])
            return self._run_locked(receipt)
        except Exception:
            if receipt["busy"]:
                receipt["asset"].update(status="failed", error="素材接收未完成；已保留收到的文件")
                self.journal.save(receipt)
                self._release(receipt)
            raise

    def _prepare(self, receipt):
        if receipt.get("prepared"):
            return
        source = self._file(receipt, "source"+receipt["suffix"])
        size, checksum = self._digest(source)
        if size != receipt["size_bytes"] or checksum != receipt["sha256"]:
            raise IntegrityError("保留的原始文件校验失败")
        info = media.inspect(source, receipt["asset"]["kind"])
        normalized, info = media.normalize(source, info, self._stage(receipt), max_output_bytes=self.max_bytes)
        specs = {"original": dict(filename=source.name, size_bytes=size, sha256=checksum,
                                   content_type=receipt["asset"]["mime"], phase="planned")}
        if normalized:
            path = self._file(receipt, normalized.name)
            model_size, model_hash = self._digest(path)
            if not model_size:
                raise media.MediaError("规范化素材为空，未上传模型副本")
            with path.open("r+b") as stream:
                os.fsync(stream.fileno())
            specs["model"] = dict(filename=path.name, size_bytes=model_size, sha256=model_hash,
                                  content_type=media.MIMES[path.suffix], phase="planned")
        _sync_directory(self._stage(receipt))
        info.update(bytes=size, sha256=checksum, mime=receipt["asset"]["mime"])
        receipt.update(objects=specs, prepared=True)
        receipt["asset"]["metadata"] = info
        self.journal.save(receipt)

    def _verify_stored(self, spec):
        info = self.store.stat(spec["key"])
        if info.size_bytes != spec["size_bytes"] or info.content_type != spec["content_type"]:
            raise IntegrityError("存储对象与素材上传记录不符")
        with self.store.open(spec["key"]) as source:
            size, checksum = _copy(source, _Sink(), spec["size_bytes"], spec["sha256"])
        if size != spec["size_bytes"]:
            raise IntegrityError("存储对象不完整")
        return ObjectInfo(info.provider, info.key, size, checksum, info.content_type, info.etag, info.version_id)

    def _write_object(self, receipt, role, *, interrupted=False):
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
            self.journal.save(receipt)
            try:
                state = self.multipart.begin(receipt["owner"], receipt["id"], receipt["id"]+"-"+role,
                    size_bytes=size, sha256=checksum, filename=spec["filename"], content_type=spec["content_type"])
                spec.update(session_id=state["id"], key=self.multipart.journal.get(receipt["owner"], state["id"])["key"])
                self.journal.save(receipt)
                state = self.multipart.reconcile(receipt["owner"], state["id"], interrupted=interrupted)
                if state["status"] == "active":
                    with path.open("rb") as source:
                        for number in range(1, state["part_count"]+1):
                            if number in state["completed_parts"]:
                                continue
                            source.seek((number-1)*state["part_size"])
                            remaining = min(state["part_size"], size-source.tell())
                            self.multipart.upload_part(receipt["owner"], state["id"], number, _PartReader(source, remaining))
                result = self.multipart.complete(receipt["owner"], state["id"])
            except MultipartOutcomeUnknown as error:
                spec.update(session_id=error.session_id, key=error.key)
                self.journal.save(receipt)
                raise
        else:
            if spec["phase"] == "planned":
                spec.update(key=make_object_key(receipt["owner"], receipt["id"], spec["filename"]), phase="putting")
                self.journal.save(receipt)
                try:
                    with path.open("rb") as source:
                        self.store.put(spec["key"], source, content_type=spec["content_type"],
                                       max_bytes=self.max_bytes, expected_sha256=checksum)
                except ObjectAlreadyExists:
                    pass
                except Exception:
                    spec["phase"] = "put_unknown"
                    self.journal.save(receipt)
                    raise StorageWriteUncertain(spec["key"]) from None
                spec["phase"] = "verifying"
                self.journal.save(receipt)
            # Unknown outcomes only read back the existing key. Never new-key retry.
            result = self._verify_stored(spec)
        receipt["asset"][role] = asdict(result)
        spec["phase"] = "verified"
        self.journal.save(receipt)

    def _run(self, receipt, *, interrupted=False):
        with self._operation(receipt["id"]):
            return self._run_locked(receipt, interrupted=interrupted)

    def _run_locked(self, receipt, *, interrupted=False):
        try:
            receipt["asset"].update(status="validating")
            receipt["asset"].pop("error", None)
            self.journal.save(receipt)
            if not receipt.get("prepared"):
                with media.PROCESSING_ADMISSION.acquire(receipt["asset"]["kind"]):
                    if receipt.get("derivation"):
                        self._prepare_derivation(receipt)
                    self._prepare(receipt)
            for role in ("original", "model"):
                if role in receipt["objects"]:
                    self._write_object(receipt, role, interrupted=interrupted)
            receipt["asset"]["status"] = "ready"
            self.journal.save(receipt)
        except Exception as error:
            uncertain = any(s["phase"] in {"putting", "put_unknown", "verifying", "multipart"} for s in receipt["objects"].values())
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
            receipt["asset"].update(status="storage_unknown" if uncertain else "failed", error=detail)
            self.journal.save(receipt)
            self._release(receipt)
            raise
        self._release(receipt)
        return self.public(receipt["asset"])

    def resume(self, owner, asset_id):
        return self.reconcile(owner, asset_id)

    def reconcile(self, owner, asset_id, *, interrupted=False, expected_version=None):
        # The local lock is taken before receipt claim and held through release.
        # An operator assertion never overrides a still-running local operation.
        asset = self.get(owner, asset_id)
        if asset["status"] == "ready" and not interrupted and expected_version is None:
            with self._operation(asset_id):
                self._cleanup_committed_asset(owner, asset_id)
            return self.public(asset)
        with self._operation(asset_id):
            return self._reconcile_locked(owner, asset_id, interrupted=interrupted,
                expected_version=expected_version)

    def _reconcile_locked(self, owner, asset_id, *, interrupted=False, expected_version=None):
        asset = self.get(owner, asset_id)
        if asset["status"] == "ready" and not interrupted and expected_version is None:
            self._cleanup_committed_asset(owner, asset_id)
            return self.public(asset)
        receipt = self.journal.get(owner, asset_id)
        if expected_version is not None and (type(expected_version) is not int or expected_version < 0
                or receipt["version"] != expected_version):
            raise AssetConflict("素材收据版本已变化；请重新只读核对")
        if receipt.get("storage_binding") != self.storage_binding:
            raise AssetConflict("素材上传属于另一存储位置或身份，未自动切换供应商")
        if not receipt.get("accepted_input") or not (receipt.get("size_bytes") or receipt.get("derivation")):
            raise AssetConflict("文件未完整接收，需要重新选择文件上传")
        if asset["status"] == "ready":
            if receipt["busy"]:
                if not interrupted:
                    return self.public(asset)
                if not receipt.get("prepared") or "original" not in receipt["objects"]:
                    raise AssetConflict("就绪素材收据证据不完整；未释放预留")
                for role, spec in receipt["objects"].items():
                    actual = asset.get(role, {})
                    if (role not in {"original", "model"} or spec.get("phase") != "verified"
                            or any(actual.get(k) != spec.get(k) for k in ("key", "size_bytes", "sha256", "content_type"))):
                        raise AssetConflict("就绪素材与已验证对象证据不一致；未释放预留")
                    self._verify_stored(spec)
                # Ready was saved before the independent quota-release commit.
                # Reconcile bytes and release once; never generate or write keys.
                self._release(receipt)
            else:
                self._cleanup_after_release(receipt)
            return self.public(asset)
        self.journal.claim(receipt, interrupted=interrupted)
        return self._run_locked(receipt, interrupted=interrupted)

    def settle_incomplete(self, owner, asset_id, *, expected_version, writers_stopped=False):
        """Operator-only: account for retained partial bytes; never make ready.

        Shared local lock blocks actual live operations. An explicit assertion
        remains necessary for old code/other hosts not sharing this local lock.
        """
        if writers_stopped is not True:
            raise AssetConflict("必须先确认所有原写入者已停止")
        self.get(owner, asset_id)
        with self._operation(asset_id):
            receipt = self.journal.get(owner, asset_id)
            if (type(expected_version) is not int or expected_version < 0
                    or receipt["version"] != expected_version):
                raise AssetConflict("素材收据版本已变化；请重新只读核对")
            if (receipt.get("storage_binding") != self.storage_binding
                    or receipt.get("suffix") not in media.EXTENSIONS):
                raise AssetConflict("不完整旧收据没有匹配的存储身份；未猜测或释放预留")
            if (receipt.get("accepted_input") or receipt.get("prepared") or receipt.get("derivation")
                    or receipt["objects"] or receipt["asset"]["status"] == "ready"):
                raise AssetConflict("此收据不是单纯未完整接收；请核对原对象或使用完整素材恢复")
            if receipt["busy"]:
                receipt["asset"].update(status="failed", error="原文件接收未完成；旧写入者已停止，已保留收到的字节，请重新选择文件上传")
                self.journal.save(receipt)
                self._release(receipt)
            return self.public(receipt["asset"])

    def get(self, owner, asset_id, project_id=None):
        clauses = [asset_table.c.id == asset_id, asset_table.c.tenant == self.tenant, asset_table.c.owner == owner]
        if project_id is not None:
            clauses.append(asset_table.c.project_id == project_id)
        with self.engine.connect() as conn:
            value = conn.execute(select(asset_table.c.record).where(*clauses)).scalar_one_or_none()
        if value is None:
            raise AssetNotFound("素材不存在")
        return json.loads(value)

    def list(self, owner, project_id):
        with self.engine.connect() as conn:
            rows = conn.execute(select(asset_table.c.record).where(asset_table.c.tenant == self.tenant,
                asset_table.c.owner == owner, asset_table.c.project_id == project_id, asset_table.c.status != "duplicate").order_by(asset_table.c.created.desc()).limit(500)).scalars()
            return [self.public(json.loads(row)) for row in rows]

    def model_snapshot(self, owner, project_id, asset_id):
        record = self.get(owner, asset_id, project_id)
        if record["status"] != "ready" or not record.get("metadata", {}).get("model_ready") or "model" not in record:
            raise media.MediaError("参考素材未就绪；长视频/音频请先选择2–15秒片段")
        return {"asset_id": asset_id, "metadata": record["metadata"], "model": record["model"],
                "original": record["original"], "parent_id": record.get("parent_id"), "selection": record.get("selection")}

    def derive(self, owner, asset_id, start, end):
        ident = uuid.uuid4().hex
        with self._operation(ident):
            return self._derive(ident, owner, asset_id, start, end)

    def _derive(self, ident, owner, asset_id, start, end):
        parent = self.get(owner, asset_id)
        if parent["status"] != "ready":
            raise media.MediaError("原素材尚未就绪")
        info = parent["metadata"]
        if info["kind"] not in {"video", "audio"}:
            raise media.MediaError("当前仅视频/音频支持时间选段")
        if (type(start) not in (int, float) or type(end) not in (int, float)
                or not math.isfinite(start) or not math.isfinite(end) or start < 0
                or end > info["source_duration"] + .01 or not 2 <= end-start <= 15):
            raise media.MediaError("选段须在原素材范围内，且长度为2–15秒")
        # Reserve the same durable capacity/concurrency slot as uploads before
        # downloading or invoking FFmpeg. The receipt also survives API restart.
        suffix = ".mp4" if info["kind"] == "video" else ".wav"
        asset = {"id": ident, "asset_id": ident, "client_asset_id": None,
            "project_id": parent["project_id"], "file_name": "trimmed"+suffix,
            "kind": info["kind"], "mime": media.MIMES[suffix], "content_type": media.MIMES[suffix],
            "created_at": time.time(), "parent_id": asset_id, "selection": {"start": start, "end": end},
            "status": "validating"}
        receipt = self.journal.create(owner, asset)
        receipt.update(suffix=suffix, storage_binding=self.storage_binding, accepted_input=True,
            derivation={"input_name": "derivation-input"+Path(parent["file_name"]).suffix.lower(),
                        "original": dict(parent["original"]), "metadata": dict(info), "phase": "planned"})
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
                with self.store.open(original["key"]) as source, receiving.open("xb") as target:
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
            expected_name = "trimmed"+receipt["suffix"]
            if (directory / expected_name).exists():
                self._file(receipt, expected_name)
            target = media.derive(source_path, spec["metadata"], directory, selected["start"], selected["end"],
                                  max_output_bytes=self.max_bytes)
            target = self._file(receipt, target.name)
            count, checksum = self._digest(target)
            if not count:
                raise media.MediaError("裁剪素材为空")
            with target.open("r+b") as stream:
                os.fsync(stream.fileno())
            destination = directory / ("source"+receipt["suffix"])
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
        fields = {"id", "asset_id", "client_asset_id", "project_id", "file_name", "kind", "mime", "content_type",
                  "created_at", "parent_id", "selection", "status", "metadata", "error"}
        clean = {k: v for k, v in record.items() if k in fields}
        clean["content_url"] = f'/v1/assets/{record["id"]}/content'
        return clean
