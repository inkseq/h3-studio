"""Durable output collection; no generation, background service, or import-time IO.

Fixed keys and reservations survive unknown PUT/MPU outcomes. Keep receipts and
verified staging; never infer absence from an expired lease or delete an orphan.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import time

from sqlalchemy import Column, Integer, MetaData, String, Table, Text, insert, select, update
from sqlalchemy.exc import IntegrityError as SQLIntegrityError

from .assets import asset_table, metadata as asset_metadata, _PartReader
from .storage import (IntegrityError, LocalObjectStore, S3ObjectStore, ObjectAlreadyExists,
    ObjectNotFound, StorageWriteUncertain, UnsupportedStorageOperation, _check_ancestors,
    _no_links, _part, validate_key)
from .storage_asset_journal import (AssetConflict, AssetUploadJournal, artifact_accounting, artifact_identity)
from .storage_multipart import MultipartJournal, MultipartUploadManager
from .storage_schema import create_storage_schema
from . import scratch_retention

MIB = 1024*1024
_schema = MetaData()
write_receipts = Table("platform_artifact_write_receipts", _schema,
    Column("id", String(64), primary_key=True), Column("tenant", String(80), nullable=False),
    Column("owner", String(80), nullable=False), Column("job_id", String(36), nullable=False),
    Column("attempt_id", String(36), nullable=False), Column("version", Integer, nullable=False),
    Column("record", Text, nullable=False))


class ArtifactWritePending(AssetConflict):
    pass


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


class ArtifactWriter:
    def __init__(self, engine, store, work_dir, *, tenant="sixnine", owner_quota_bytes=10*1024*MIB,
                 tenant_quota_bytes=40*1024*MIB, max_object_bytes=512*MIB):
        self.engine, self.store, self.tenant = engine, store, _part(tenant)
        self.work_dir = Path(work_dir)
        if not self.work_dir.is_absolute():
            raise ValueError("artifact_work_directory_must_be_absolute")
        _check_ancestors(self.work_dir)
        if type(max_object_bytes) is not int or not 1 <= max_object_bytes <= 512*MIB:
            raise ValueError("artifact_size_limit_invalid")
        self.max_bytes = max_object_bytes
        if not store.capabilities.conditional_create:
            raise UnsupportedStorageOperation("Artifact collection requires conditional object creation")
        if isinstance(store, LocalObjectStore):
            identity = {"provider": "local", "root": str(store.root)}
        elif isinstance(store, S3ObjectStore):
            identity = {k: getattr(store.config, k) for k in
                        ("provider", "endpoint_url", "region", "bucket", "service", "profile")}
        else:
            raise UnsupportedStorageOperation("Artifact store requires a stable private identity")
        self.binding = _hash(identity)
        create_storage_schema(engine, asset_metadata, _schema)
        self.quota = AssetUploadJournal(engine, asset_table, tenant=tenant, reservation_bytes=1,
            owner_quota_bytes=owner_quota_bytes, tenant_quota_bytes=tenant_quota_bytes,
            max_active_per_owner=2, max_active_total=4)
        self.multipart = (MultipartUploadManager(store, MultipartJournal(engine, tenant), max_object_bytes=max_object_bytes)
                          if isinstance(store, S3ObjectStore) else None)

    def _identity(self, job, attempt_id, tag):
        if job["tenant_id"] != self.tenant:
            raise ArtifactWritePending("artifact_tenant_mismatch")
        for value in (job["owner_id"], job["id"], attempt_id, tag):
            _part(value)
        from .inference.outputs import delivery_spec
        delivery = delivery_spec(job)
        fingerprint = [job["request"], tag]
        if delivery is not None:
            fingerprint.append(delivery)
        return _hash([self.tenant, job["owner_id"], job["id"], attempt_id]), _hash(fingerprint)

    def get(self, job, attempt_id, tag):
        ident, fingerprint = self._identity(job, attempt_id, tag)
        with self.engine.connect() as conn:
            row = conn.execute(select(write_receipts).where(write_receipts.c.id == ident)).first()
        if row is None:
            return None
        record = json.loads(row.record)
        if (record["fingerprint"] != fingerprint or record["binding"] != self.binding
                or record["staging_root"] != str(self.work_dir)):
            raise ArtifactWritePending("artifact_receipt_identity_mismatch")
        if record.get("collection_route") == "gpu-r2-v1" and (
                record.get("owner") != job["owner_id"] or record.get("tag") != tag or record.get("id") != ident):
            raise ArtifactWritePending("artifact_receipt_identity_mismatch")
        return dict(record, version=row.version)

    def _path(self, record, role):
        name = role["filename"]
        if name not in ("verified.mp4", "verified.flac"):
            raise IntegrityError("artifact_staging_name_invalid")
        path = self.work_dir / _part(record["tag"]) / name
        _check_ancestors(path.parent)
        _no_links(path)
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise IntegrityError("artifact_staging_not_regular")
        return path

    def _digest(self, path):
        digest, size = hashlib.sha256(), 0
        with path.open("rb") as handle:
            initial = os.fstat(handle.fileno())
            if not stat.S_ISREG(initial.st_mode) or initial.st_nlink != 1:
                raise IntegrityError("artifact_staging_not_regular")
            while chunk := handle.read(MIB):
                size += len(chunk)
                if size > self.max_bytes:
                    raise IntegrityError("artifact_output_exceeds_limit")
                digest.update(chunk)
            final = os.fstat(handle.fileno())
        if not size or (initial.st_size, initial.st_mtime_ns) != (final.st_size, final.st_mtime_ns) or size != final.st_size:
            raise IntegrityError("artifact_staging_changed")
        return size, digest.hexdigest()

    def begin_staging(self, job, attempt_id, tag, *, kinds=("video",)):
        """Bound collection disk use BEFORE fetch/FFmpeg. No object writes here.

        Reserve raw download + verified output + future immutable store object.
        prepare() later shrinks this to verified actual bytes in one transaction.
        Crashed/failed staging keeps its receipt and conservative reservation.
        """
        existing = self.get(job, attempt_id, tag)
        if existing is not None:
            return existing
        if tuple(kinds) not in (("video",), ("video", "audio")):
            raise IntegrityError("artifact_roles_invalid")
        ident, fingerprint = self._identity(job, attempt_id, tag)
        record = dict(id=ident, owner=job["owner_id"], tag=tag, fingerprint=fingerprint,
            binding=self.binding, staging_root=str(self.work_dir), phase="staging", roles={},
            expected_roles=list(kinds), reserved_bytes=3*self.max_bytes*len(kinds))
        if isinstance(self.store, S3ObjectStore) and self.store.config.provider == "r2":
            record["scratch_policy"] = scratch_retention.POLICY
        self.quota._ensure(record["owner"])
        try:
            with self.engine.begin() as conn:
                self.quota._change(conn, record["owner"], record["reserved_bytes"], 0)
                conn.execute(insert(write_receipts).values(id=ident, tenant=self.tenant, owner=record["owner"],
                    job_id=job["id"], attempt_id=attempt_id, version=0, record=json.dumps(record)))
        except SQLIntegrityError:
            return self.get(job, attempt_id, tag)
        return dict(record, version=0)

    def _remote_route(self, job=None, record=None):
        if not isinstance(self.store, S3ObjectStore) or self.store.config.provider != "r2":
            raise UnsupportedStorageOperation("GPU artifact delivery requires the selected R2 store")
        if job is not None:
            from .inference.outputs import gpu_delivery
            if not gpu_delivery(job):
                raise IntegrityError("artifact_gpu_delivery_not_frozen")
        if record is not None and record.get("collection_route") != "gpu-r2-v1":
            raise ArtifactWritePending("artifact_collection_route_mismatch")
        if record is not None:
            if record.get("binding") != self.binding or record.get("staging_root") != str(self.work_dir):
                raise ArtifactWritePending("artifact_receipt_identity_mismatch")
            for kind, role in record["roles"].items():
                if (kind not in {"video", "audio"}
                        or role.get("key") != f'owners/{_part(record["owner"])}/assets/{_part(record["tag"])}/result.'
                            + ("mp4" if kind == "video" else "flac")
                        or type(role.get("size_bytes")) is not int or not 1 <= role["size_bytes"] <= self.max_bytes
                        or role.get("status") not in {"planned", "putting", "unknown", "verified"}):
                    raise IntegrityError("artifact_remote_role_identity_mismatch")

    def begin_remote(self, job, attempt_id, tag, *, kinds=("video",)):
        """Reserve final R2 objects before GPU encoding; no CPU media staging."""
        self._remote_route(job)
        from .inference.outputs import _shape
        expected = ("video", "audio") if _shape(job)[3] else ("video",)
        if tuple(kinds) != expected:
            raise IntegrityError("artifact_roles_invalid")
        existing = self.get(job, attempt_id, tag)
        if existing is not None:
            self._remote_route(record=existing)
            if existing.get("expected_roles") != list(expected):
                raise IntegrityError("artifact_roles_changed_after_reservation")
            return existing
        ident, fingerprint = self._identity(job, attempt_id, tag)
        record = dict(id=ident, owner=job["owner_id"], tag=tag, fingerprint=fingerprint,
            binding=self.binding, staging_root=str(self.work_dir), phase="staging", roles={},
            collection_route="gpu-r2-v1", expected_roles=list(expected),
            reserved_bytes=self.max_bytes*len(expected), raw_staging_bytes=0)
        self.quota._ensure(record["owner"])
        try:
            with self.engine.begin() as conn:
                self.quota._change(conn, record["owner"], record["reserved_bytes"], 0)
                conn.execute(insert(write_receipts).values(id=ident, tenant=self.tenant, owner=record["owner"],
                    job_id=job["id"], attempt_id=attempt_id, version=0, record=json.dumps(record)))
        except SQLIntegrityError:
            return self.begin_remote(job, attempt_id, tag, kinds=kinds)
        return dict(record, version=0)

    def prepare_remote(self, job, attempt_id, tag, task_id, description):
        """Freeze sealed GPU output identities in the original collection receipt."""
        self._remote_route(job)
        from .inference.outputs import _request, _shape, delivery_spec, validate_delivery_evidence
        from .inference.wangp_delivery import validate_delivery_receipt
        description = validate_delivery_receipt(description)
        if description["state"] not in {"prepared", "uploading", "uploaded", "rejected", "unknown"}:
            raise ArtifactWritePending("artifact_gpu_delivery_not_sealed")
        identity = description["identity"]
        width, height, _, audio = _shape(job)
        if (description["owner_id"] != job["owner_id"] or identity["job_id"] != job["id"] or identity["attempt_tag"] != tag
                or identity["operation_id"] != task_id or identity["request_hash"] != job["request_hash"]
                or identity["manifest_digest"] != job["execution_plan"]["engine_manifest_digest"]
                or description["delivery_spec"] != delivery_spec(job)
                or description["export_crf"] != _request(job).get("export_crf", 18)
                or description["generate_audio"] is not audio):
            raise IntegrityError("artifact_gpu_delivery_identity_mismatch")
        existing = self.get(job, attempt_id, tag)
        if existing is None:
            raise ArtifactWritePending("artifact_remote_reservation_missing")
        self._remote_route(record=existing)
        if set(description["roles"]) != set(existing["expected_roles"]):
            raise IntegrityError("artifact_roles_changed_after_reservation")
        roles = {}
        for kind, value in description["roles"].items():
            if value["size_bytes"] > self.max_bytes:
                raise IntegrityError("artifact_output_exceeds_limit")
            evidence = value["evidence"]
            validate_delivery_evidence(job, evidence, kind)
            if kind == "video" and (evidence.get("width") != width or evidence.get("height") != height
                    or evidence.get("has_audio") is not audio):
                raise IntegrityError("artifact_gpu_media_shape_mismatch")
            if kind == "audio" and evidence.get("has_audio") is not True:
                raise IntegrityError("artifact_gpu_audio_evidence_missing")
            role = dict(value, status="planned", key=validate_key(
                f'owners/{existing["owner"]}/assets/{tag}/result.' + ("mp4" if kind == "video" else "flac")))
            roles[kind] = role
        if any(value["key"] != roles[kind]["key"] or value["storage_binding"] != self.binding
               for kind, value in description["objects"].items()):
            raise IntegrityError("artifact_gpu_storage_binding_mismatch")
        sealed = {name: description[name] for name in (
            "version", "fingerprint", "identity", "owner_id", "delivery_spec", "export_crf", "generate_audio", "roles")}
        if existing["phase"] != "staging":
            if existing.get("gpu_delivery") != sealed:
                raise IntegrityError("artifact_gpu_sealed_outputs_changed")
            return existing
        record = dict(existing, phase="reserved", roles=roles, gpu_delivery=sealed,
                      reserved_bytes=sum(role["size_bytes"] for role in roles.values()))
        self._save(record, byte_delta=record["reserved_bytes"]-existing["reserved_bytes"])
        return record

    def remote_targets(self, record, heartbeat=lambda: None, *, upload_stopped=False):
        """Short lived capabilities only; durable records never contain URLs.

        A missing object after a prior intent does not prove the PUT stopped.
        Only the bound GPU delivery receipt can positively fence a new attempt.
        """
        self._remote_route(record=record)
        if record["phase"] != "reserved" or not record["roles"]:
            raise ArtifactWritePending("artifact_not_ready_for_storage")
        pending = []
        for kind, role in record["roles"].items():
            try:
                self._verify_remote(role, heartbeat)
            except ObjectNotFound:
                if role["status"] != "planned" and upload_stopped is not True:
                    raise ArtifactWritePending("artifact_put_unknown_keep_reserved_key") from None
                pending.append(kind)
            else:
                if role["status"] != "verified":
                    role["status"] = "verified"
                    self._save(record)
        targets = {kind: {"object_key": role["key"], "size_bytes": role["size_bytes"],
            "sha256": role["sha256"], "content_md5": role["content_md5"],
            "content_type": role["content_type"], "storage_binding": self.binding,
            "upload": self.store.presign_artifact_upload(role["key"], content_type=role["content_type"],
                size_bytes=role["size_bytes"], content_md5=role["content_md5"])}
            for kind in pending for role in (record["roles"][kind],)}
        if pending:
            for kind in pending:
                record["roles"][kind]["status"] = "putting"
            # Signing above is local only. Commit before the caller can send
            # any capability to the GPU, while a signer failure stays planned.
            self._save(record)
        return targets

    def remote_unknown(self, record):
        self._remote_route(record=record)
        changed = False
        for role in record["roles"].values():
            if role["status"] == "putting":
                role["status"] = "unknown"
                changed = True
        if changed:
            self._save(record)

    def verify_remote(self, record, heartbeat=lambda: None):
        """HEAD and bounded streaming SHA only; never download to CPU disk."""
        self._remote_route(record=record)
        if record["phase"] not in {"reserved", "settled"} or not record["roles"]:
            raise ArtifactWritePending("artifact_not_ready_for_storage")
        if record["phase"] == "settled" and any(role["status"] != "verified" for role in record["roles"].values()):
            raise ArtifactWritePending("artifact_settlement_evidence_missing")
        missing = False
        for role in record["roles"].values():
            try:
                self._verify_remote(role, heartbeat)
            except ObjectNotFound:
                # Preserve complete roles even if the missing role sorts first.
                # Their own readback may resolve an unknown upload acknowledgement.
                missing = True
                continue
            if role["status"] != "verified":
                role["status"] = "verified"
                self._save(record)
        if missing:
            raise ObjectNotFound("artifact_remote_roles_missing")
        return self._specs(record)

    def prepare(self, job, attempt_id, tag, files, *, raw_paths=()):
        existing = self.get(job, attempt_id, tag)
        if existing is not None and existing["phase"] != "staging":
            return existing
        ident, fingerprint = self._identity(job, attempt_id, tag)
        record = dict(id=ident, owner=job["owner_id"], tag=tag, fingerprint=fingerprint,
            binding=self.binding, staging_root=str(self.work_dir), phase="reserved", roles={}, reserved_bytes=0)
        if existing is not None:
            record["expected_roles"] = existing["expected_roles"]
        policy = (existing.get("scratch_policy") if existing is not None else
                  scratch_retention.POLICY if isinstance(self.store, S3ObjectStore)
                  and self.store.config.provider == "r2" else None)
        if policy is not None:
            record["scratch_policy"] = policy
        for kind, path, mime, evidence in files:
            if kind not in ("video", "audio") or kind in record["roles"]:
                raise IntegrityError("artifact_roles_invalid")
            filename = "verified.mp4" if kind == "video" else "verified.flac"
            if mime != ("video/mp4" if kind == "video" else "audio/flac"):
                raise IntegrityError("artifact_content_type_invalid")
            role = dict(filename=filename, status="planned", content_type=mime)
            expected = self._path(record, role)
            if Path(path) != expected:
                raise IntegrityError("artifact_staging_path_mismatch")
            size, sha = self._digest(expected)
            allowed = {"width", "height", "duration_s", "fps", "has_audio"}
            from .inference.outputs import delivery_spec, validate_delivery_evidence, NATIVE_EVIDENCE_FIELDS
            delivery = delivery_spec(job)
            if delivery is not None:
                allowed |= NATIVE_EVIDENCE_FIELDS
            if not isinstance(evidence, dict) or set(evidence)-allowed:
                raise IntegrityError("artifact_evidence_invalid")
            validate_delivery_evidence(job, evidence, kind)
            role.update(size_bytes=size, sha256=sha, evidence=evidence,
                        key=validate_key(f'owners/{record["owner"]}/assets/{tag}/result.' + ("mp4" if kind == "video" else "flac")))
            record["roles"][kind] = role
            record["reserved_bytes"] += 2*size  # Remote/local object plus retained verified staging.
        if "video" not in record["roles"]:
            raise IntegrityError("artifact_video_missing")
        if existing is not None and set(record["roles"]) != set(existing["expected_roles"]):
            raise IntegrityError("artifact_roles_changed_after_reservation")
        # Comfy fetch retains these original downloads in the collection directory.
        # CPU backend's separate attempt cache has its own 8 GiB root reservation.
        record["raw_staging_bytes"] = 0
        for name in ("raw.mp4", "raw.flac"):
            path = self.work_dir / tag / name
            if path.exists() or path.is_symlink():
                _no_links(path)
                info = path.stat()
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > self.max_bytes:
                    raise IntegrityError("artifact_raw_staging_invalid")
                record["raw_staging_bytes"] += info.st_size
        if policy == scratch_retention.POLICY:
            directory = self.work_dir / tag
            # WanGP returns opaque task/hash filenames, not Comfy's raw.mp4.
            # Track only this attempt directory; e.g. CPU mock cache is separate.
            paths = {directory / role["filename"] for role in record["roles"].values()}
            paths.update(Path(path) for path in raw_paths if Path(path).parent == directory)
            paths.update(directory / name for name in ("raw.mp4", "raw.flac") if (directory / name).exists())
            manifest = scratch_retention.manifest(directory, sorted(paths), self.max_bytes)
            if any((manifest[role["filename"]]["size_bytes"], manifest[role["filename"]]["sha256"])
                   != (role["size_bytes"], role["sha256"]) for role in record["roles"].values()):
                raise IntegrityError("r2_artifact_scratch_hash_mismatch")
            verified_names = {role["filename"] for role in record["roles"].values()}
            record["raw_staging_bytes"] = sum(value["size_bytes"] for name, value in manifest.items()
                                               if name not in verified_names)
            record["scratch_cleanup"] = dict(phase="pending", files=manifest,
                bytes=sum(value["size_bytes"] for value in manifest.values()))
        record["reserved_bytes"] += record["raw_staging_bytes"]
        self.quota._ensure(record["owner"])
        if existing is not None:
            with self.engine.begin() as conn:
                self.quota._change(conn, record["owner"], record["reserved_bytes"]-existing["reserved_bytes"], 0)
                changed = conn.execute(update(write_receipts).where(write_receipts.c.id == ident,
                    write_receipts.c.version == existing["version"]).values(
                        version=existing["version"]+1, record=json.dumps(record)))
                if changed.rowcount != 1:
                    raise ArtifactWritePending("artifact_staging_state_changed")
            return dict(record, version=existing["version"]+1)
        try:
            with self.engine.begin() as conn:
                self.quota._change(conn, record["owner"], record["reserved_bytes"], 0)
                conn.execute(insert(write_receipts).values(id=ident, tenant=self.tenant, owner=record["owner"],
                    job_id=job["id"], attempt_id=attempt_id, version=0, record=json.dumps(record)))
        except SQLIntegrityError:
            return self.get(job, attempt_id, tag)
        return dict(record, version=0)

    def _save(self, record, *, byte_delta=0):
        value = {k: v for k, v in record.items() if k != "version"}
        value["updated_at"] = time.time()
        with self.engine.begin() as conn:
            if byte_delta:
                self.quota._change(conn, record["owner"], byte_delta, 0)
            result = conn.execute(update(write_receipts).where(write_receipts.c.id == record["id"],
                write_receipts.c.version == record["version"]).values(record=json.dumps(value), version=record["version"]+1))
            if result.rowcount != 1:
                raise ArtifactWritePending("artifact_collection_already_in_progress")
        record["version"] += 1

    def _verify_remote(self, role, heartbeat):
        info = self.store.stat(role["key"])
        if info.size_bytes != role["size_bytes"] or info.content_type != role["content_type"]:
            raise IntegrityError("artifact_remote_metadata_mismatch")
        digest, count = hashlib.sha256(), 0
        with self.store.open(role["key"]) as stream:
            while chunk := stream.read(min(MIB, role["size_bytes"]-count+1)):
                count += len(chunk)
                if count > role["size_bytes"]:
                    raise IntegrityError("artifact_remote_size_mismatch")
                digest.update(chunk)
                heartbeat()
        if count != role["size_bytes"] or digest.hexdigest() != role["sha256"]:
            raise IntegrityError("artifact_remote_hash_mismatch")

    def write(self, record, heartbeat=lambda: None, *, fenced=False):
        """fenced=True is operator-only proof the previous network worker stopped.

        Never derive this flag from a client request or lease expiry. Without it,
        crashed in-flight MPU intents stay reserved for manual reconciliation.
        """
        if record["phase"] == "settled" and record["roles"]:
            # A post-commit replay needs original remote bytes, not scratch that
            # was successfully removed. It never publishes/settles again.
            if any(role["status"] != "verified" for role in record["roles"].values()):
                raise ArtifactWritePending("artifact_settlement_evidence_missing")
            for role in record["roles"].values():
                self._verify_remote(role, heartbeat)
            return self._specs(record)
        if record["phase"] != "reserved" or not record["roles"]:
            raise ArtifactWritePending("artifact_not_ready_for_storage")
        for kind, role in record["roles"].items():
            heartbeat()
            # Recovery checks the exact preserved input. It never recreates media.
            if self._digest(self._path(record, role)) != (role["size_bytes"], role["sha256"]):
                raise IntegrityError("artifact_staging_hash_mismatch")
            if role["status"] == "verified":
                self._verify_remote(role, heartbeat)
                continue
            try:
                self._verify_remote(role, heartbeat)
            except ObjectNotFound:
                if role["status"] in ("putting", "unknown"):
                    raise ArtifactWritePending("artifact_put_unknown_keep_reserved_key") from None
                if isinstance(self.store, S3ObjectStore) and role["size_bytes"] > self.store.config.max_single_put_bytes:
                    role["status"] = "multipart"
                    self._save(record)
                    status = self.multipart.begin(record["owner"], record["tag"], record["id"][:40]+"-"+kind,
                        size_bytes=role["size_bytes"], sha256=role["sha256"], filename=role["filename"],
                        content_type=role["content_type"], object_key=role["key"])
                    role["multipart_id"] = status["id"]
                    self._save(record)
                    # Never infer an interrupted provider request merely from lease expiry.
                    # Explicit *_unknown states reconcile; in-flight states require fencing.
                    status = self.multipart.reconcile(record["owner"], status["id"], interrupted=fenced)
                    if status["status"] == "active":
                        with self._path(record, role).open("rb") as source:
                            for number in range(1, status["part_count"]+1):
                                if number in status["completed_parts"]:
                                    source.seek(min(number*status["part_size"], role["size_bytes"]))
                                    continue
                                heartbeat()
                                self.multipart.upload_part(record["owner"], status["id"], number,
                                    _PartReader(source, min(status["part_size"], role["size_bytes"]-source.tell())))
                    self.multipart.complete(record["owner"], status["id"])
                else:
                    role["status"] = "putting"
                    self._save(record)  # Intent committed before the first possible object write.
                    try:
                        with self._path(record, role).open("rb") as source:
                            self.store.put(role["key"], source, content_type=role["content_type"],
                                max_bytes=self.max_bytes, expected_sha256=role["sha256"])
                    except (ObjectAlreadyExists, StorageWriteUncertain):
                        role["status"] = "unknown"
                        self._save(record)
                        raise ArtifactWritePending("artifact_put_needs_readback") from None
                self._verify_remote(role, heartbeat)
            role["status"] = "verified"
            self._save(record)
        return self._specs(record)

    @staticmethod
    def _specs(record):
        return [{"kind": kind, "object_key": role["key"], "size_bytes": role["size_bytes"],
                 "sha256": role["sha256"], "validated": True, "content_type": role["content_type"], **role["evidence"]}
                for kind, role in record["roles"].items()]

    def cleanup_settled(self, job, attempt_id, tag):
        """After complete commits, under the original slot lock, never on unknown.

        Missing manifest files after a crash are still accounted until this CAS
        commits; repeat calls cannot release their bytes a second time.
        """
        record = self.get(job, attempt_id, tag)
        if (not isinstance(self.store, S3ObjectStore) or self.store.config.provider != "r2"
                or record is None or record.get("scratch_policy") != scratch_retention.POLICY
                or record["phase"] != "settled"):
            return
        cleanup = record.get("scratch_cleanup")
        if cleanup is None or cleanup["phase"] == "complete":
            return
        if any(role["status"] != "verified" for role in record["roles"].values()):
            raise IntegrityError("r2_artifact_scratch_evidence_missing")
        for role in record["roles"].values():
            self._verify_remote(role, lambda: None)
        remote_bytes = sum(role["size_bytes"] for role in record["roles"].values())
        if record["reserved_bytes"] != remote_bytes + cleanup["bytes"]:
            raise IntegrityError("r2_artifact_scratch_accounting_mismatch")
        directory = self.work_dir / _part(tag)
        scratch_retention.remove_manifest(directory, cleanup["files"], self.max_bytes)
        record["reserved_bytes"] = remote_bytes
        cleanup.update(phase="complete", completed_at=time.time())
        self._save(record, byte_delta=-cleanup["bytes"])
        if directory.exists():
            directory.rmdir()

    def settlement(self, record):
        """Callback for queue.complete's existing DB transaction (no storage IO)."""
        def settle(conn, specs):
            row = conn.execute(select(write_receipts).where(write_receipts.c.id == record["id"]).with_for_update()).one()
            current = json.loads(row.record)
            if (row.version != record["version"] or current["phase"] != "reserved"
                    or any(role["status"] != "verified" for role in current["roles"].values())):
                raise ArtifactWritePending("artifact_settlement_state_changed")
            expected = {role["key"]: (role["size_bytes"], role["sha256"]) for role in current["roles"].values()}
            if {spec["object_key"]: (spec["size_bytes"], spec["sha256"]) for spec in specs} != expected or len(specs) != len(expected):
                raise IntegrityError("artifact_settlement_evidence_mismatch")
            for spec in specs:
                ident, size, sha = artifact_identity(spec, record["owner"])
                for scope, _, _ in self.quota._scopes(record["owner"]):
                    conn.execute(insert(artifact_accounting).values(scope=scope, object_id=ident, size_bytes=size, sha256=sha))
            # Reservation equals verified object + retained staging bytes, so the
            # settlement changes attribution, not the counter. No phantom release.
            current["phase"] = "settled"
            conn.execute(update(write_receipts).where(write_receipts.c.id == record["id"], write_receipts.c.version == row.version)
                         .values(version=row.version+1, record=json.dumps(current)))
        return settle
