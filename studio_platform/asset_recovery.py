"""Explicit LocalObjectStore upload recovery; list is read-only by default.

No cloud adapter, credential loader, model worker or API server is constructed.
The recovery assertion must come from an operator who has stopped all prior API
writers. A timer, browser request or local lock alone is not that proof.
"""
from __future__ import annotations

from .asset_objects import ASSET_OBJECT_ROLES

import argparse
from dataclasses import replace
import json
import os
from pathlib import Path

from sqlalchemy import inspect, select

from .assets import AssetService
from .repository import Repository
from .settings import Settings
from .storage import LocalObjectStore, _check_ancestors, _part
from .storage_asset_journal import receipts


def list_receipts(engine, *, tenant, owner, project_id=None, limit=20, offset=0):
    """Bounded safe metadata only; no keys, files, content or credential fields."""
    _part(tenant)
    _part(owner)
    if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or not 0 <= offset <= 100000:
        raise ValueError("invalid_asset_recovery_pagination")
    clauses = [receipts.c.tenant == tenant, receipts.c.owner == owner]
    if project_id is not None:
        clauses.append(receipts.c.project_id == project_id)
    with engine.connect() as connection:
        if engine.dialect.name == "postgresql":
            connection.exec_driver_sql("SET TRANSACTION READ ONLY")
        elif engine.dialect.name == "sqlite":
            connection.exec_driver_sql("PRAGMA query_only=ON")
        else:
            raise ValueError("unsupported_asset_recovery_database")
        # This engine is private to this CLI operation and closed afterwards.
        try:
            rows = list(connection.execute(select(receipts.c.id, receipts.c.project_id, receipts.c.version,
                receipts.c.record).where(*clauses).order_by(receipts.c.id).offset(offset).limit(limit+1)).mappings())
        finally:
            if engine.dialect.name == "sqlite":
                connection.exec_driver_sql("PRAGMA query_only=OFF")
    items = []
    for row in rows[:limit]:
        record = json.loads(row["record"])
        asset = record.get("asset", {})
        items.append({"asset_id": row["id"], "project_id": row["project_id"], "receipt_version": row["version"],
            "status": asset.get("status", "unknown"), "busy": record.get("busy") is True,
            "accepted_input": record.get("accepted_input") is True, "prepared": record.get("prepared") is True,
            "storage_binding_known": isinstance(record.get("storage_binding"), str),
            "reserved_bytes": record.get("reserved"), "updated_at": record.get("updated_at"),
            "object_phases": {k: v.get("phase", "unknown") for k, v in record.get("objects", {}).items()
                if k in ASSET_OBJECT_ROLES}})
    return {"state": "read_only", "tenant": tenant, "owner": owner, "items": items,
        "has_more": len(rows) > limit, "next_offset": offset+limit if len(rows) > limit else None,
        "cloud_calls_enabled": False}


def recover_asset(service, *, owner, project_id, asset_id, receipt_version, assert_writers_stopped=False,
                  settle_incomplete=False):
    if assert_writers_stopped is not True:
        raise ValueError("operator_must_confirm_all_previous_writers_stopped")
    if not isinstance(service.store, LocalObjectStore):
        raise ValueError("asset_recovery_requires_local_storage")
    if type(receipt_version) is not int or receipt_version < 0:
        raise ValueError("explicit_asset_receipt_version_required")
    service.get(owner, asset_id, project_id)  # Ownership + exact project before any claim.
    if settle_incomplete:
        value = service.settle_incomplete(owner, asset_id, expected_version=receipt_version, writers_stopped=True)
    else:
        value = service.reconcile(owner, asset_id, interrupted=True, expected_version=receipt_version)
    receipt = service.journal.get(owner, asset_id)
    complete = value["status"] == "ready" and receipt["busy"] is False
    settled = settle_incomplete and value["status"] == "failed" and receipt["busy"] is False
    return {"state": "recovered" if complete else "incomplete_settled" if settled else "incomplete",
        "media_ready": complete, "retained_bytes": receipt["reserved"], "asset_id": asset_id, "project_id": project_id,
        "status": value["status"], "receipt_version": receipt["version"], "busy": receipt["busy"],
        "same_receipt": True, "cloud_calls_enabled": False}


def main(argv=None):
    parser = argparse.ArgumentParser(description="List or explicitly recover stopped Local asset uploads; no generation/cloud calls")
    parser.add_argument("--mode", choices=("list", "recover", "settle-incomplete"), default="list")
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--owner", required=True)
    parser.add_argument("--project-id")
    parser.add_argument("--asset-id")
    parser.add_argument("--receipt-version", type=int)
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--assert-writers-stopped", action="store_true",
        help="Operator confirms ALL previous API writers stopped, including other hosts/older code; not a timeout proof")
    args = parser.parse_args(argv)
    repo = None
    try:
        # Reject an unauthorised mutation before Settings, DB or media setup.
        if args.mode != "list" and (not args.assert_writers_stopped or not args.project_id
                or not args.asset_id or args.receipt_version is None):
            raise ValueError("explicit_fenced_single_asset_recovery_required")
        settings = Settings.from_environment()
        if args.data_dir:
            if not args.data_dir.is_absolute():
                raise ValueError("asset_data_path_must_be_absolute")
            configured = settings.database_url if (os.environ.get("SIXNINE_DATABASE_URL")
                or os.environ.get("SIXNINE_DATABASE_URL_FILE")) else ""
            settings = replace(settings, data_dir=args.data_dir, database_url=configured)
        if args.tenant != settings.tenant_id or settings.storage_provider != "local":
            raise ValueError("exact_tenant_and_local_storage_required")
        _check_ancestors(settings.data_dir)
        if not settings.data_dir.is_dir():
            raise ValueError("asset_data_directory_must_exist")
        if settings.database_url.startswith("sqlite:///"):
            from sqlalchemy.engine import make_url
            target = make_url(settings.database_url).database
            if not target or target == ":memory:" or not Path(target).is_file():
                raise ValueError("asset_database_must_exist")
            _check_ancestors(Path(target))
        repo = Repository(settings.database_url)
        if args.mode == "list":
            result = list_receipts(repo.engine, tenant=args.tenant, owner=args.owner,
                project_id=args.project_id, limit=args.limit, offset=args.offset)
        else:
            required_tables = {"platform_assets", "platform_asset_upload_receipts",
                "platform_asset_storage_quota", "platform_artifact_storage_accounting"}
            if not required_tables.issubset(set(inspect(repo.engine).get_table_names())):
                raise ValueError("asset_recovery_schema_must_already_exist")
            # Refuse an unrelated existing DB/asset before constructing services
            # or creating any directories. Recheck the version under the OS lock.
            with repo.engine.connect() as connection:
                selected = connection.execute(select(receipts.c.version).where(receipts.c.tenant == args.tenant,
                    receipts.c.owner == args.owner, receipts.c.project_id == args.project_id,
                    receipts.c.id == args.asset_id)).scalar_one_or_none()
            if selected is None or selected != args.receipt_version:
                raise ValueError("current_owned_receipt_must_exist")
            # These must already be this service's actual private roots.
            for relative in ("objects", "asset-staging"):
                path = settings.data_dir / relative
                _check_ancestors(path)
                if not path.is_dir():
                    raise ValueError("asset_storage_or_staging_must_exist")
            store = LocalObjectStore(settings.data_dir / "objects")
            service = AssetService(repo.engine, store, settings.data_dir, tenant=settings.tenant_id,
                max_bytes=settings.max_upload_bytes)
            result = recover_asset(service, owner=args.owner, project_id=args.project_id,
                asset_id=args.asset_id, receipt_version=args.receipt_version,
                assert_writers_stopped=args.assert_writers_stopped, settle_incomplete=args.mode == "settle-incomplete")
        print(json.dumps(result, ensure_ascii=True, sort_keys=True))
        return 0 if args.mode == "list" or result["state"] in {"recovered", "incomplete_settled"} else 2
    except Exception:
        # No exception repr, DSN, signed URL, storage key or source text escapes.
        print(json.dumps({"state": "asset_recovery_refused", "cloud_calls_enabled": False,
            "detail": "Verify stopped writers, exact tenant/owner/project, receipt version, original storage and integrity"}))
        return 1
    finally:
        if repo is not None:
            repo.close()


if __name__ == "__main__":
    raise SystemExit(main())
