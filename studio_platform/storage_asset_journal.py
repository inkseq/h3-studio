"""Private upload receipts and atomic capacity counters; no storage/network IO."""
from __future__ import annotations

import json
import hashlib
import re
import time
import uuid

from sqlalchemy import BigInteger, Column, Integer, MetaData, String, Table, Text, UniqueConstraint, insert, inspect, select, update
from sqlalchemy.exc import IntegrityError as SQLIntegrityError

from .repository import BudgetExceeded, Conflict
from .storage import _part, key_belongs_to
from .storage_schema import create_storage_schema
from .content_lifecycle import (assert_content_available, assert_project_writable,
    content_is_deleted, owner_reference_gate)

_schema = MetaData()
receipts = Table("platform_asset_upload_receipts", _schema,
    Column("id", String(32), primary_key=True), Column("tenant", String(80), nullable=False),
    Column("owner", String(80), nullable=False), Column("project_id", String(160), nullable=False),
    Column("client_key", String(128), nullable=True), Column("version", Integer, nullable=False),
    Column("record", Text, nullable=False),
    UniqueConstraint("tenant", "owner", "project_id", "client_key", name="uq_asset_upload_client"))
quotas = Table("platform_asset_storage_quota", _schema,
    Column("scope", String(320), primary_key=True), Column("bytes", BigInteger, nullable=False),
    Column("active", Integer, nullable=False))
artifact_accounting = Table("platform_artifact_storage_accounting", _schema,
    Column("scope", String(320), primary_key=True), Column("object_id", String(64), primary_key=True),
    Column("size_bytes", BigInteger, nullable=False), Column("sha256", String(64), nullable=False))


def artifact_identity(spec, owner):
    key, size, sha = spec.get("object_key"), spec.get("size_bytes"), spec.get("sha256")
    if (not isinstance(key, str) or not key_belongs_to(key, owner) or type(size) is not int or size <= 0
            or not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{64}", sha)
            or spec.get("validated") is not True):
        raise AssetConflict("产物存储元信息需要核对；未假设零字节或释放配额")
    return hashlib.sha256(key.encode()).hexdigest(), size, sha


class AssetConflict(Conflict):
    pass


class AssetQuotaExceeded(BudgetExceeded):
    pass


def validate_client_asset_id(value):
    """Opaque database idempotency ID, never an object key or filesystem path.

    The frontend sends entity_id:file_id. Windows device-name/path rules do not
    apply here; SQL remains parameterized and physical keys use server UUIDs.
    """
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:\-]{0,127}", value):
        raise ValueError("客户端素材ID须为1–128个ASCII字母、数字、点、下划线、冒号或短横线")
    return value


class AssetUploadJournal:
    def __init__(self, engine, assets, *, tenant, reservation_bytes, owner_quota_bytes,
                 tenant_quota_bytes, max_active_per_owner, max_active_total):
        self.engine, self.assets, self.tenant = engine, assets, _part(tenant)
        limits = (reservation_bytes, owner_quota_bytes, tenant_quota_bytes, max_active_per_owner, max_active_total)
        if any(type(n) is not int or n < 1 for n in limits):
            raise ValueError("素材配额和并发上限必须为正整数")
        self.reservation = reservation_bytes
        self.owner_limit, self.tenant_limit = owner_quota_bytes, tenant_quota_bytes
        self.owner_active, self.total_active = max_active_per_owner, max_active_total
        create_storage_schema(engine, _schema)  # Additive only; existing asset rows are untouched.

    def _scopes(self, owner):
        _part(owner)
        return ((f"tenant:{self.tenant}", self.tenant_limit, self.total_active),
                (f"tenant:{self.tenant}:owner:{owner}", self.owner_limit, self.owner_active))

    def _available(self, conn, owner, asset, *, receipt=None, writable=False):
        """The caller owns the asset OS lock before opening a write transaction."""
        owner_reference_gate(conn, self.tenant, owner)
        selection = asset.get("selection") or {}
        keys = [asset[role]["key"] for role in ("original", "model")
                if isinstance(asset.get(role), dict) and asset[role].get("key")]
        if receipt is not None:
            keys.extend(spec["key"] for spec in receipt.get("objects", {}).values() if spec.get("key"))
            keys.extend(receipt[name]["original"]["key"] for name in ("derivation", "frame_extraction")
                        if receipt.get(name, {}).get("original", {}).get("key"))
        assert_content_available(conn, self.tenant, owner,
            asset_ids=tuple(value for value in (asset.get("id"), asset.get("parent_id")) if value),
            artifact_ids=(selection["artifact_id"],) if selection.get("artifact_id") else (),
            object_keys=keys, storage_binding=receipt.get("storage_binding") if receipt else None)
        if writable:
            assert_project_writable(conn, self.tenant, owner, asset["project_id"])

    def _ensure(self, owner):
        for scope, _, _ in self._scopes(owner):
            try:
                with self.engine.begin() as conn:
                    owner_reference_gate(conn, self.tenant, owner)
                    if conn.execute(select(quotas.c.scope).where(quotas.c.scope == scope)).first():
                        continue
                    clauses = [self.assets.c.tenant == self.tenant, receipts.c.id.is_(None)]
                    if scope != self._scopes(owner)[0][0]:
                        clauses.append(self.assets.c.owner == owner)
                    legacy = conn.execute(select(self.assets.c.record).select_from(
                        self.assets.outerjoin(receipts, self.assets.c.id == receipts.c.id)).where(*clauses)).scalars()
                    known_bytes = 0
                    for raw in legacy:
                        record = json.loads(raw)
                        for role in ("original", "model"):
                            size = record.get(role, {}).get("size_bytes", 0)
                            if type(size) is not int or size < 0:
                                raise AssetConflict("旧素材大小元信息需要核对，未重置配额")
                            known_bytes += size
                    conn.execute(insert(quotas).values(scope=scope, bytes=known_bytes, active=0))
            except SQLIntegrityError:
                pass
        self._backfill_artifacts(owner)

    def _backfill_artifacts(self, owner):
        """Add old artifact evidence once; never overwrite live reservations.

        Both scopes have independent markers so later-created owner counters can
        be seeded even if the tenant counter already included those objects.
        Historical actual usage can exceed the cap; subsequent reservations fail.
        """
        if not inspect(self.engine).has_table("platform_artifacts"):
            return
        from .repository import artifacts, jobs
        for scope, _, _ in self._scopes(owner):
            clauses = [jobs.c.tenant_id == self.tenant]
            if scope != self._scopes(owner)[0][0]:
                clauses.append(jobs.c.owner_id == owner)
            with self.engine.connect() as conn:
                rows = list(conn.execute(select(artifacts.c.id, artifacts.c.metadata, jobs.c.owner_id)
                    .select_from(artifacts.join(jobs, artifacts.c.job_id == jobs.c.id)).where(*clauses)))
            for artifact_id, spec, artifact_owner in rows:
                try:
                    with self.engine.begin() as conn:
                        owner_reference_gate(conn, self.tenant, artifact_owner)
                        if (content_is_deleted(conn, self.tenant, artifact_owner, artifact_id=artifact_id)
                                or content_is_deleted(conn, self.tenant, artifact_owner,
                                    object_key=spec.get("object_key"))):
                            continue
                        ident, size, sha = artifact_identity(spec, artifact_owner)
                        row = conn.execute(select(artifact_accounting).where(
                            artifact_accounting.c.scope == scope, artifact_accounting.c.object_id == ident)).first()
                        if row is not None:
                            if (row.size_bytes, row.sha256) != (size, sha):
                                raise AssetConflict("相同产物存储键存在冲突证据；未重置配额")
                            continue
                        conn.execute(insert(artifact_accounting).values(scope=scope, object_id=ident, size_bytes=size, sha256=sha))
                        conn.execute(update(quotas).where(quotas.c.scope == scope).values(bytes=quotas.c.bytes + size))
                except SQLIntegrityError:
                    # Concurrent identical backfill wins one marker and one increment.
                    with self.engine.connect() as conn:
                        row = conn.execute(select(artifact_accounting).where(
                            artifact_accounting.c.scope == scope, artifact_accounting.c.object_id == ident)).one()
                    if (row.size_bytes, row.sha256) != (size, sha):
                        raise AssetConflict("产物配额证据冲突") from None

    def _change(self, conn, owner, byte_delta, active_delta):
        for scope, cap, active_cap in self._scopes(owner):
            where = [quotas.c.scope == scope, quotas.c.bytes + byte_delta >= 0, quotas.c.active + active_delta >= 0]
            if byte_delta > 0:
                where.append(quotas.c.bytes + byte_delta <= cap)
            if active_delta > 0:
                where.append(quotas.c.active + active_delta <= active_cap)
            result = conn.execute(update(quotas).where(*where).values(
                bytes=quotas.c.bytes + byte_delta, active=quotas.c.active + active_delta))
            if result.rowcount != 1:
                raise AssetQuotaExceeded("素材容量或处理并发已达到上限")

    def usage(self, owner):
        self._ensure(owner)
        with self.engine.connect() as conn:
            row = conn.execute(select(quotas).where(quotas.c.scope == self._scopes(owner)[1][0])).one()
        return dict(accounted_bytes=row.bytes, active_uploads=row.active,
                    limit_bytes=self.owner_limit, max_active=self.owner_active)

    def create(self, owner, asset):
        self._ensure(owner)
        receipt = dict(id=asset["id"], owner=owner, asset=asset, objects={}, busy=True,
                       reserved=self.reservation, operation_id=uuid.uuid4().hex, updated_at=time.time())
        with self.engine.begin() as conn:
            self._available(conn, owner, asset, writable=True)
            self._change(conn, owner, self.reservation, 1)
            conn.execute(insert(receipts).values(id=asset["id"], tenant=self.tenant, owner=owner,
                project_id=asset["project_id"], client_key=None, version=0, record=json.dumps(receipt)))
            conn.execute(insert(self.assets).values(id=asset["id"], tenant=self.tenant, owner=owner,
                project_id=asset["project_id"], status=asset["status"], created=asset["created_at"], record=json.dumps(asset)))
        receipt["version"] = 0
        return receipt

    def get(self, owner, asset_id, *, include_deleted=False):
        """include_deleted is private audit access, never a public ready replay."""
        with self.engine.begin() as conn:
            owner_reference_gate(conn, self.tenant, owner)
            row = conn.execute(select(receipts).where(receipts.c.id == asset_id,
                receipts.c.tenant == self.tenant, receipts.c.owner == owner)).first()
            if row is None:
                raise AssetConflict("此素材没有可恢复上传记录；旧素材保持不变")
            record = json.loads(row.record)
            if not include_deleted:
                self._available(conn, owner, record["asset"], receipt=record)
            record["version"] = row.version
        return record

    def save(self, receipt, *, byte_delta=0, active_delta=0, client_key=None):
        value = {k: v for k, v in receipt.items() if k != "version"}
        value["updated_at"] = time.time()
        version = receipt["version"]
        with self.engine.begin() as conn:
            self._available(conn, receipt["owner"], receipt["asset"], receipt=receipt, writable=True)
            if byte_delta or active_delta:
                self._change(conn, receipt["owner"], byte_delta, active_delta)
            updates = dict(record=json.dumps(value, ensure_ascii=False), version=version+1)
            if client_key is not None:
                updates["client_key"] = client_key
            changed = conn.execute(update(receipts).where(receipts.c.id == receipt["id"],
                receipts.c.owner == receipt["owner"], receipts.c.tenant == self.tenant,
                receipts.c.version == version).values(**updates))
            if changed.rowcount != 1:
                raise AssetConflict("素材处理状态已变化；请刷新后重试")
            conn.execute(update(self.assets).where(self.assets.c.id == receipt["id"],
                self.assets.c.owner == receipt["owner"], self.assets.c.tenant == self.tenant)
                .values(record=json.dumps(value["asset"], ensure_ascii=False), status=value["asset"]["status"]))
        receipt.update(version=version+1, updated_at=value["updated_at"])

    def bind(self, receipt, client_key):
        receipt["accepted_input"] = True
        if client_key is None:
            self.save(receipt)
            return None
        validate_client_asset_id(client_key)
        try:
            self.save(receipt, client_key=client_key)
            return None
        except SQLIntegrityError:
            with self.engine.connect() as conn:
                ident = conn.execute(select(receipts.c.id).where(receipts.c.tenant == self.tenant,
                    receipts.c.owner == receipt["owner"], receipts.c.project_id == receipt["asset"]["project_id"],
                    receipts.c.client_key == client_key)).scalar_one_or_none()
            if ident is None:
                raise
            existing = self.get(receipt["owner"], ident)
            if existing.get("fingerprint") != receipt["fingerprint"]:
                receipt["accepted_input"] = False
                raise AssetConflict("此客户端素材ID已用于不同内容；请使用新的素材ID") from None
            return existing

    def legacy_match(self, receipt, client_key):
        if client_key is None:
            return None
        with self.engine.begin() as conn:
            self._available(conn, receipt["owner"], receipt["asset"], receipt=receipt, writable=True)
            rows = conn.execute(select(self.assets.c.record).select_from(
                self.assets.outerjoin(receipts, self.assets.c.id == receipts.c.id)).where(
                    self.assets.c.tenant == self.tenant, self.assets.c.owner == receipt["owner"],
                    self.assets.c.project_id == receipt["asset"]["project_id"], receipts.c.id.is_(None))).scalars()
            values = [json.loads(raw) for raw in rows]
            for asset in values:
                if asset.get("client_asset_id") != client_key:
                    continue
                self._available(conn, receipt["owner"], asset)
                info = asset.get("metadata", {})
                if (asset.get("status") != "ready" or info.get("sha256") != receipt["sha256"]
                        or info.get("bytes") != receipt["size_bytes"] or asset.get("mime") != receipt["asset"]["mime"]
                        or asset.get("parent_id") != receipt["asset"].get("parent_id")
                        or asset.get("selection") != receipt["asset"].get("selection")):
                    raise AssetConflict("旧素材客户端ID不能确认相同内容；请使用新的素材ID")
                return asset
        return None

    def claim(self, receipt, *, interrupted=False):
        if receipt["busy"] and not interrupted:
            raise AssetConflict("素材可能仍在处理，请先等待；若服务重启后状态持续不变，请联系管理员恢复同一素材，不要反复上传")
        # Operator-only: caller must actually fence/stop the old worker. Never
        # infer this from a timer or expose interrupted directly to API clients.
        active_delta = 0 if receipt["busy"] else 1
        delta = max(0, self.reservation - receipt["reserved"])
        receipt.update(busy=True, reserved=receipt["reserved"]+delta, operation_id=uuid.uuid4().hex)
        self.save(receipt, byte_delta=delta, active_delta=active_delta)

    def release(self, receipt, retained_bytes):
        if not receipt["busy"]:
            return
        delta = retained_bytes - receipt["reserved"]
        receipt.update(busy=False, reserved=retained_bytes)
        self.save(receipt, byte_delta=delta, active_delta=-1)
