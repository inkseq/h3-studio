"""Snapshot-owned media references, coverage and final backup publication."""

from __future__ import annotations
from contextlib import closing
import hashlib
import json
import os
import re
import time
from .asset_objects import ASSET_OBJECT_ROLES
from .backup_missing import missing_object
from .backup_loss_policy import EXPECTED_MISSING
from .backup_schema import MIB, AUTH_TABLES, BackupError
from .backup_files import _digest, _open_sqlite_readonly, _write_json
from .storage import (
    LocalObjectStore,
    ObjectNotFound,
    _copy,
    _sync_directory,
    key_belongs_to,
)


def _store_binding(store, provider):
    if provider == "local":
        identity = {"provider": "local", "root": str(store.root)}
    else:
        config = getattr(store, "config", None)
        identity = {
            k: getattr(config, k, None)
            for k in (
                "provider",
                "endpoint_url",
                "region",
                "bucket",
                "service",
                "profile",
            )
        }
        if identity["provider"] != provider:
            raise BackupError("backup_artifact_storage_binding_mismatch")
    return hashlib.sha256(
        json.dumps(
            identity, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


def _media_references(db, provider_bindings=None, *, offline=False):
    found, pending = {}, 0

    def add(owner, info, reference):
        key, size, sha, mime = (
            info.get("key", info.get("object_key")),
            info.get("size_bytes"),
            info.get("sha256"),
            info.get("content_type"),
        )
        if (
            not isinstance(key, str)
            or not key_belongs_to(key, owner)
            or type(size) is not int
            or not 0 < size <= 512 * MIB
            or not isinstance(sha, str)
            or not re.fullmatch(r"[0-9a-f]{64}", sha)
            or not isinstance(mime, str)
            or len(mime) > 128
            or info.get("provider", "local") not in {"local", "r2"}
        ):
            raise BackupError("backup_media_reference_requires_review")
        item = dict(
            key=key, size_bytes=size, sha256=sha, content_type=mime, owner=owner
        )
        # Old Local manifests intentionally retain their exact format. Remote
        # identity is bound into new manifests and checked against the DB copy.
        if info.get("provider", "local") != "local":
            item["provider"] = info["provider"]
        if "storage_binding" in info:
            item["storage_binding"] = info["storage_binding"]
        if key in found:
            prior = found[key]
            if (
                any(prior.get(k) != value for k, value in item.items())
                or prior.get("provider", "local") != item.get("provider", "local")
                or prior.get("storage_binding") != item.get("storage_binding")
            ):
                raise BackupError("backup_media_reference_conflict")
        entry = found.setdefault(key, dict(item, references=[]))
        entry["references"].append(reference)

    for ident, owner, project, status, raw in db.execute(
        "SELECT id,owner,project_id,status,record FROM platform_assets"
    ):
        record = json.loads(raw)
        if status != "ready" or record.get("rendition_status") == "pending":
            pending += 1
        for role in ASSET_OBJECT_ROLES:
            if record.get(role):
                add(
                    owner,
                    record[role],
                    {
                        "table": "assets",
                        "id": ident,
                        "project_id": project,
                        "role": role,
                    },
                )
    receipt_table = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='platform_artifact_write_receipts'"
    ).fetchone()
    for ident, owner, project, tenant, job, attempt, raw in db.execute(
        "SELECT a.id,j.owner_id,j.project_id,j.tenant_id,a.job_id,a.attempt_id,a.metadata "
        "FROM platform_artifacts a JOIN platform_jobs j ON j.id=a.job_id"
    ):
        spec = json.loads(raw)
        if spec.get("validated") is not True:
            raise BackupError("backup_artifact_not_validated")
        # Historic GPU uploads omit provider in artifact metadata. Only their
        # original immutable receipt can identify R2; configuration is no proof.
        rows = (
            db.execute(
                "SELECT id,tenant,owner,record FROM platform_artifact_write_receipts "
                "WHERE job_id=? AND attempt_id=?",
                (job, attempt),
            ).fetchall()
            if receipt_table
            else []
        )
        if (
            not rows
            and "provider" not in spec
            and provider_bindings
            and "r2" in provider_bindings.values()
            and not offline
        ):
            raise BackupError("backup_artifact_provider_requires_review")
        remote = []
        for receipt_id, receipt_tenant, receipt_owner, record_raw in rows:
            record = json.loads(record_raw)
            route = record.get("collection_route")
            if route is None and (
                provider_bindings is None
                or offline
                and record.get("binding") not in provider_bindings
            ):
                continue  # Original Local manifests predate provider evidence.
            if route not in (None, "gpu-r2-v1"):
                raise BackupError("backup_artifact_provider_requires_review")
            role = record.get("roles", {}).get(spec["kind"], {})
            expected_id = hashlib.sha256(
                json.dumps(
                    [tenant, owner, job, attempt],
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode()
            ).hexdigest()
            if (
                receipt_tenant != tenant
                or receipt_owner != owner
                or receipt_id != expected_id
                or record.get("id") != receipt_id
                or record.get("owner") != owner
                or role.get("status") != "verified"
                or role.get("key") != spec.get("object_key")
                or any(
                    role.get(k) != spec.get(k)
                    for k in ("size_bytes", "sha256", "content_type")
                )
                or not isinstance(record.get("binding"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", record["binding"])
            ):
                raise BackupError("backup_artifact_receipt_mismatch")
            provider = (
                "r2"
                if route == "gpu-r2-v1"
                else (provider_bindings or {}).get(record["binding"])
            )
            if (
                provider not in {"local", "r2"}
                or spec.get("provider", provider) != provider
            ):
                raise BackupError("backup_artifact_storage_binding_mismatch")
            if provider == "r2":
                remote.append(record["binding"])
        if remote:
            if len(remote) != 1 or spec.get("provider", "r2") != "r2":
                raise BackupError("backup_artifact_receipt_mismatch")
            spec = dict(spec, provider="r2", storage_binding=remote[0])
        add(
            owner,
            spec,
            {
                "table": "artifacts",
                "id": ident,
                "project_id": project,
                "role": spec["kind"],
            },
        )
        if spec.get('kind') == 'video':
            # Auxiliary copies are held by the SAME original output receipt.
            # Frozen unresolved keys are included, not silently omitted by a
            # backup that happens between PUT and publication.
            for _receipt_id, receipt_tenant, receipt_owner, record_raw in rows:
                record = json.loads(record_raw)
                display = record.get('display', {})
                if not display:
                    continue
                if (
                    record.get('roles', {}).get('video', {}).get('key')
                    != spec.get('object_key')
                    or receipt_tenant != tenant
                    or receipt_owner != owner
                ):
                    raise BackupError('backup_artifact_receipt_mismatch')
                if (
                    display.get('phase') not in {'complete', 'missing'}
                    or display.get('scratch_cleanup', {}).get('phase') != 'complete'
                ):
                    pending += 1
                for role, item in display.get('objects', {}).items():
                    if (
                        role not in {'thumb', 'preview'}
                        or item.get('content_type') != 'image/webp'
                    ):
                        raise BackupError('backup_artifact_receipt_mismatch')
                    auxiliary = dict(item, object_key=item['key'], validated=True)
                    if remote:
                        auxiliary.update(provider='r2', storage_binding=remote[0])
                    add(
                        owner,
                        auxiliary,
                        {
                            'table': 'artifacts',
                            'id': ident,
                            'project_id': project,
                            'role': role,
                        },
                    )
    return found, pending


def _publish_media_backup(
    output,
    object_root,
    references,
    pending,
    counts,
    *,
    source_database,
    stores=None,
    allow_missing_media=False,
    classify_expected_deletions=False,
    hosted_users=None,
):
    media = output / "media"
    media.mkdir(mode=0o700)
    stores = stores if stores is not None else {"local": LocalObjectStore(object_root)}
    if (
        type(allow_missing_media) is not bool
        or type(classify_expected_deletions) is not bool
        or classify_expected_deletions
        and not allow_missing_media
    ):
        raise BackupError("backup_opt_in_invalid")
    objects, missing = [], []
    evidence = {}
    if classify_expected_deletions:
        from .backup_deletion import deletion_evidence

        with closing(_open_sqlite_readonly(output / "database.sqlite3")) as db:
            evidence = deletion_evidence(db, references)
    for key, item in sorted(references.items()):
        provider = item.get("provider", "local")
        if provider not in stores:
            raise BackupError("backup_source_provider_not_configured")
        store = stores[provider]
        if "storage_binding" in item:
            if _store_binding(store, provider) != item["storage_binding"]:
                raise BackupError("backup_artifact_storage_binding_mismatch")
        try:
            info = store.stat(key)
        except ObjectNotFound:
            if not allow_missing_media:
                raise
            missing.append(
                missing_object(
                    item, store, evidence if classify_expected_deletions else None
                )
            )
            continue
        # Presigned GPU PUTs lack optional HEAD SHA metadata. Every body still
        # must match the original DB/receipt SHA and size before publication.
        if (
            info.provider != provider
            or info.key != key
            or (info.size_bytes, info.content_type)
            != (item["size_bytes"], item["content_type"])
            or info.sha256 is not None
            and info.sha256 != item["sha256"]
        ):
            raise BackupError("backup_object_metadata_mismatch")
        filename = hashlib.sha256(key.encode("ascii")).hexdigest() + ".bin"
        with store.open(key) as source, (media / filename).open("xb") as target:
            if os.name != "nt":
                os.fchmod(target.fileno(), 0o600)
            size, sha = _copy(source, target, item["size_bytes"], item["sha256"])
            if size != item["size_bytes"]:
                raise BackupError("backup_object_size_mismatch")
            target.flush()
            os.fsync(target.fileno())
        objects.append(dict(item, file=filename))
    manifest = dict(
        version=1,
        kind="sixnine-local-business-backup",
        created_at=time.time(),
        source_database=source_database,
        restoration_target="isolated-sqlite-and-local-objects",
        database_sha256=_digest(output / "database.sqlite3"),
        tables=counts,
        objects=objects,
        media_bytes=sum(item["size_bytes"] for item in objects),
        pending_assets=pending,
        excluded=list(sorted(AUTH_TABLES)),
        staging_included=False,
        orphan_objects_included=False,
        execution_restore="hold",
        native_postgres_restore=False,
    )
    if allow_missing_media:
        manifest.update(
            version=3 if classify_expected_deletions else 2,
            missing_objects=missing,
            coverage=_coverage(
                objects, missing, classified=classify_expected_deletions
            ),
        )
    if (output / 'workflow-state.json').exists():
        from .workflow_backup import inspect_file

        manifest['workflow_state'] = inspect_file(output)
    if hosted_users is not None:
        from .workos_backup import FILE, inspect_file, validate

        _write_json(output / FILE, validate(hosted_users))
        manifest['hosted_users'] = inspect_file(output)
    _write_json(output / "manifest.json", manifest)  # Success marker written last.
    _sync_directory(media)
    _sync_directory(output)
    result = {
        "state": "backed_up",
        "objects": len(objects),
        "media_bytes": manifest["media_bytes"],
        "pending_assets": pending,
    }
    if 'hosted_users' in manifest:
        result['hosted_users_state'] = manifest['hosted_users']['status']
    if allow_missing_media:
        result.update(
            coverage=manifest["coverage"],
            state="archived_incomplete" if missing else "backed_up",
        )
    return result


def _coverage(objects, missing, *, classified=False):
    available_bytes = sum(x["size_bytes"] for x in objects)
    missing_bytes = sum(x["size_bytes"] for x in missing)
    result = dict(
        status="incomplete" if missing else "complete",
        referenced_objects=len(objects) + len(missing),
        available_objects=len(objects),
        missing_objects=len(missing),
        referenced_bytes=available_bytes + missing_bytes,
        available_bytes=available_bytes,
        missing_bytes=missing_bytes,
    )
    if classified:
        expected = [x for x in missing if x['classification'] in EXPECTED_MISSING]
        result.update(
            expected_missing_objects=len(expected),
            expected_missing_bytes=sum(x['size_bytes'] for x in expected),
            unexpected_missing_objects=len(missing) - len(expected),
            unexpected_missing_bytes=missing_bytes
            - sum(x['size_bytes'] for x in expected),
            loss_alert_required=len(missing) != len(expected),
        )
    return result
