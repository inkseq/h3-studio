"""Explicit owner-scoped image-display backfill; default plan has no storage IO.

Apply addresses one original receipt/version. It does not scan prefixes, adopt
legacy assets, delete originals, generate videos or construct cloud controllers.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re

from sqlalchemy import inspect, select

from .assets import AssetService, asset_table
from .media_renditions import public_renditions, public_artifact_renditions
from .repository import Repository, artifacts, jobs
from .settings import Settings
from .storage import LocalObjectStore, S3ObjectStore, _check_ancestors, _part
from .storage_asset_journal import receipts
from .storage_config import S3StorageConfig, load_configured_storage_credentials


def plan(engine, *, tenant, owner, limit=100, offset=0):
    """Metadata-only owner census, including an honest missing-receipt count."""
    _part(tenant); _part(owner)
    if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or not 0 <= offset <= 100000:
        raise ValueError("rendition_invalid_pagination")
    with engine.connect() as connection:
        rows = list(connection.execute(select(asset_table.c.id, asset_table.c.record,
            receipts.c.version, receipts.c.record.label("receipt_record")).select_from(
            asset_table.outerjoin(receipts, (asset_table.c.id == receipts.c.id)
                & (asset_table.c.owner == receipts.c.owner) & (asset_table.c.tenant == receipts.c.tenant)))
            .where(asset_table.c.owner == owner, asset_table.c.tenant == tenant, asset_table.c.status == "ready")
            .order_by(asset_table.c.id).offset(offset).limit(limit+1)).mappings())
    items = []
    for row in rows[:limit]:
        asset = json.loads(row["record"])
        if asset.get("kind") not in {"image", "video"}:
            continue
        info = public_renditions(asset)
        receipt = json.loads(row["receipt_record"]) if row["receipt_record"] else None
        reason = ("ready" if info["rendition_ready"] else "missing_original_receipt" if receipt is None
            else "writer_active" if receipt.get("busy") else "backfill_required")
        items.append(dict(asset_id=row["id"], project_id=asset["project_id"],
            receipt_version=row["version"], rendition_status=info["rendition_status"], reason_code=reason))
    # Offline census reports the whole owner's coverage, not just this page.
    # No storage calls or attempted adoption of legacy missing receipts.
    categories = ('total','ready','missing_original_receipt','writer_active','backfill_required','unsupported')
    coverage = {kind:{name:0 for name in categories} for kind in ('assets','artifacts')}
    with engine.connect() as connection:
        for row in connection.execute(select(asset_table.c.record,receipts.c.record.label('receipt_record'))
                .select_from(asset_table.outerjoin(receipts,(asset_table.c.id==receipts.c.id)
                    & (asset_table.c.owner==receipts.c.owner) & (asset_table.c.tenant==receipts.c.tenant)))
                .where(asset_table.c.owner==owner,asset_table.c.tenant==tenant,asset_table.c.status=='ready')).mappings():
            value=json.loads(row['record']);state=coverage['assets'];state['total']+=1
            receipt=json.loads(row['receipt_record']) if row['receipt_record'] else None
            reason=('unsupported' if value.get('kind') not in {'image','video'} else
                'ready' if public_renditions(value).get('rendition_ready') else
                'missing_original_receipt' if receipt is None else
                'writer_active' if receipt.get('busy') else 'backfill_required')
            state[reason]+=1
    output_items=[]
    from .artifact_writer import write_receipts
    if inspect(engine).has_table(write_receipts.name):
        with engine.connect() as connection:
            for row in connection.execute(select(artifacts,write_receipts.c.version.label('receipt_version'),
                    write_receipts.c.record.label('receipt_record')).select_from(artifacts.join(jobs,jobs.c.id==artifacts.c.job_id)
                    .outerjoin(write_receipts,(write_receipts.c.job_id==artifacts.c.job_id)
                        & (write_receipts.c.attempt_id==artifacts.c.attempt_id)
                        & (write_receipts.c.owner==jobs.c.owner_id) & (write_receipts.c.tenant==jobs.c.tenant_id)))
                    .where(jobs.c.tenant_id==tenant,jobs.c.owner_id==owner,jobs.c.status=='succeeded')
                    .order_by(artifacts.c.id)).mappings():
                if row['metadata'].get('kind')!='video':continue
                state=coverage['artifacts'];state['total']+=1
                receipt=json.loads(row['receipt_record']) if row['receipt_record'] else None
                reason=('ready' if public_artifact_renditions(row).get('rendition_ready') else
                    'missing_original_receipt' if receipt is None else 'writer_active'
                    if receipt.get('phase')!='settled' or receipt.get('display',{}).get('phase') not in (None,'missing','complete')
                    else 'backfill_required')
                state[reason]+=1
                if offset<=state['total']-1<offset+limit:
                    output_items.append(dict(artifact_id=row['id'],job_id=row['job_id'],receipt_version=row['receipt_version'],reason_code=reason))
    return dict(state="read_only", items=items, artifacts=output_items, coverage=coverage,
        coverage_population="ready_asset_rows_and_succeeded_video_artifacts",
        next_offset=offset+limit if len(rows)>limit else None,
        artifacts_next_offset=offset+limit if coverage['artifacts']['total']>offset+limit else None,
        storage_calls_enabled=False, originals_retained=True)


def main(argv=None, *, credentials_loader=load_configured_storage_credentials, store_factory=S3ObjectStore):
    parser = argparse.ArgumentParser(description="Plan or explicitly backfill one existing media receipt; never delete originals")
    parser.add_argument("action", choices=("plan", "apply"))
    parser.add_argument("--owner", required=True)
    parser.add_argument("--asset-id")
    parser.add_argument("--artifact-id")
    parser.add_argument("--work-dir",type=Path,help="Existing original artifact writer staging root; exact receipt match required")
    parser.add_argument("--receipt-version", type=int)
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--assert-writers-stopped", action="store_true")
    args = parser.parse_args(argv)
    repo = None
    try:
        if args.action == "apply" and (bool(args.asset_id)==bool(args.artifact_id) or args.receipt_version is None
                or args.artifact_id and args.work_dir is None or args.asset_id and args.work_dir is not None):
            raise ValueError("rendition_exact_receipt_required")
        if args.action == "plan" and (args.asset_id or args.artifact_id or args.work_dir or args.receipt_version is not None or args.assert_writers_stopped):
            raise ValueError("rendition_plan_must_be_read_only")
        settings = Settings.from_environment()
        _check_ancestors(settings.data_dir)
        if not settings.data_dir.is_dir():
            raise ValueError("rendition_data_directory_must_exist")
        if settings.database_url.startswith("sqlite:///"):
            from sqlalchemy.engine import make_url
            target = make_url(settings.database_url).database
            if not target or target == ":memory:" or not Path(target).is_file():
                raise ValueError("rendition_database_must_exist")
            _check_ancestors(Path(target))
        repo = Repository(settings.database_url)
        required = {"platform_assets", "platform_asset_upload_receipts", "platform_asset_storage_quota",
                    "platform_artifact_storage_accounting"}
        if not required <= set(inspect(repo.engine).get_table_names()):
            raise ValueError("rendition_existing_schema_required")
        if args.action == "plan":
            result = plan(repo.engine, tenant=settings.tenant_id, owner=args.owner,
                          limit=args.limit, offset=args.offset)
        else:
            # Refuse an unrelated/stale identity before constructing a store or
            # loading the existing protected runtime/central credentials.
            with repo.engine.connect() as connection:
                if args.asset_id:
                    version = connection.execute(select(receipts.c.version).where(receipts.c.tenant == settings.tenant_id,
                        receipts.c.owner == args.owner, receipts.c.id == args.asset_id)).scalar_one_or_none()
                else:
                    from .artifact_writer import write_receipts
                    row=connection.execute(select(write_receipts,artifacts.c.id.label('artifact_id')).select_from(
                        write_receipts.join(artifacts,(artifacts.c.job_id==write_receipts.c.job_id)
                            & (artifacts.c.attempt_id==write_receipts.c.attempt_id)).join(jobs,jobs.c.id==artifacts.c.job_id))
                        .where(artifacts.c.id==args.artifact_id,jobs.c.tenant_id==settings.tenant_id,
                            jobs.c.owner_id==args.owner,write_receipts.c.tenant==settings.tenant_id,
                            write_receipts.c.owner==args.owner)).mappings().first()
                    version=row['version'] if row else None
                    record=json.loads(row['record']) if row else {}
                    if record.get('staging_root')!=str(args.work_dir.absolute()) or record.get('phase')!='settled':
                        raise ValueError('rendition_original_artifact_receipt_required')
            if version != args.receipt_version:
                raise ValueError("rendition_current_owned_receipt_required")
            if settings.storage_provider == "local":
                store = LocalObjectStore(settings.data_dir / "objects")
            elif settings.storage_provider == "r2":
                config = S3StorageConfig("r2", settings.storage_endpoint, settings.storage_region,
                    settings.storage_bucket, "cloudflare-r2", settings.storage_profile, enabled=True)
                store = store_factory(config, credentials_loader(config, credentials_file=settings.storage_credential_file))
            else:
                raise ValueError("rendition_storage_not_qualified")
            if args.asset_id:
                service = AssetService(repo.engine, store, settings.data_dir,
                    tenant=settings.tenant_id, max_bytes=settings.max_upload_bytes)
                value = service.ensure_renditions(args.owner, args.asset_id,
                    expected_version=args.receipt_version, interrupted=args.assert_writers_stopped)
                current = service.journal.get(args.owner, args.asset_id)
                result = dict(state="complete" if value["rendition_ready"] else "pending", asset_id=args.asset_id,
                    receipt_version=current["version"], rendition_status=value["rendition_status"],
                    same_receipt=True, originals_retained=True)
            else:
                from .artifact_writer import ArtifactWriter
                from .artifact_renditions import ensure
                job=repo.get_job_for_owner(settings.tenant_id,args.owner,row['job_id'])
                writer=ArtifactWriter(repo.engine,store,args.work_dir,tenant=settings.tenant_id)
                value=ensure(writer,job,row['attempt_id'],record['tag'],expected_version=args.receipt_version,
                    interrupted=args.assert_writers_stopped)
                result=dict(state=value['display']['phase'],artifact_id=args.artifact_id,receipt_version=value['version'],
                    reason_code=value['display'].get('error_code'),same_receipt=True,originals_retained=True)
        print(json.dumps(result, sort_keys=True))
        return 0
    except Exception as error:
        code = str(error) if re.fullmatch(r"(?:rendition|artifact_poster)_[a-z_]+|artifact_remote_(?:hash|size|metadata)_mismatch",
            str(error)) else "rendition_backfill_unconfirmed"
        print(json.dumps(dict(state="refused", reason_code=code, originals_retained=True), sort_keys=True))
        return 1
    finally:
        if repo is not None:
            repo.close()


if __name__ == "__main__":
    raise SystemExit(main())
