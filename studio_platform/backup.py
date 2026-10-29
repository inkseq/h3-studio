"""Explicit private local backups. No import-time IO, credentials, or services.

Only published media referenced by the consistent DB snapshot are copied. Live
auth tables, sessions, runtime files, environment, and arbitrary directories are
never exported. Recovery is an isolated, disabled copy, not automatic failover.
"""

from __future__ import annotations

from .asset_objects import ASSET_OBJECT_ROLES
from .backup_missing import missing_object
from .backup_loss_policy import EXPECTED_MISSING, valid_classification
from .schema_version import (
    PAYMENT_REVISION,
    LIVE_PAYMENT_REVISION,
    CREDIT_ADMISSION_REVISIONS,
)

import csv
from contextlib import closing
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
import subprocess
import time
from urllib.parse import urlsplit, parse_qs, unquote

from sqlalchemy import create_engine, text, JSON
from sqlalchemy.engine import URL

from .storage import (
    LocalObjectStore,
    ObjectNotFound,
    _check_ancestors,
    _no_links,
    _copy,
    _sync_directory,
    key_belongs_to,
)

MIB = 1024 * 1024
AUTH_TABLES = frozenset(
    {
        "platform_accounts",
        "platform_sessions",
        "platform_service_clients",
        "platform_login_limits",
        "platform_personal_api_keys",
        "platform_agent_connections",
        "platform_agent_exchange_limits",
        "platform_agent_connection_audit",
    }
)


class BackupError(Exception):
    """Stable code only; never attach DB URLs, subprocess output, or secrets."""


def _schemas(
    *,
    include_identity=True,
    include_credits=True,
    include_payments=True,
    payment_revision=PAYMENT_REVISION,
):
    # Register the same additive business tables as the application, without
    # constructing services or creating/migrating a source schema.
    from . import operator_capacity, capacity_market, dstack_workspace  # noqa: F401 -- register additive backup schema tables
    from .repository import metadata
    from .assets import metadata as assets
    from .storage_asset_journal import _schema as quota
    from .storage_multipart import _metadata as multipart
    from .artifact_writer import _schema as writer
    from .batches import metadata as batches
    from .guided import metadata as guided
    from .quick_chat import metadata as quick_chat
    from .project_activity import metadata as project_activity
    from .identity_schema import metadata as identity
    from .credit_schema import metadata as credits
    from .payment_schema import frozen_metadata as payment_metadata

    if payment_revision in CREDIT_ADMISSION_REVISIONS:
        from sqlalchemy import MetaData
        from .migrations.versions.v20261022_0007_credit_admission import extend

        original = metadata
        metadata = MetaData()
        for table in original.tables.values():
            table.to_metadata(metadata)
        extend(metadata)
    payments = payment_metadata(revision=payment_revision)

    return (
        (
            metadata,
            assets,
            quota,
            multipart,
            writer,
            batches,
            guided,
            quick_chat,
            project_activity,
        )
        + ((identity,) if include_identity else ())
        + ((credits,) if include_credits else ())
        + ((payments,) if include_payments else ())
    )


def _tables(
    *,
    include_identity=True,
    include_credits=True,
    include_payments=True,
    payment_revision=PAYMENT_REVISION,
):
    return {
        table.name: table
        for schema in _schemas(
            include_identity=include_identity,
            include_credits=include_credits,
            include_payments=include_payments,
            payment_revision=payment_revision,
        )
        for table in schema.sorted_tables
    }


def _absolute(path):
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts:
        raise BackupError("backup_paths_must_be_explicit_absolute")
    _check_ancestors(path)
    return path


def _regular(path, *, max_bytes=None):
    path = _absolute(path)
    info = _no_links(path)
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or (max_bytes is not None and info.st_size > max_bytes)
    ):
        raise BackupError("backup_file_type_or_size_invalid")
    return path


def _private_new_directory(path):
    path = _absolute(path)
    if path.exists() or path.is_symlink() or not path.parent.is_dir():
        raise BackupError(
            "backup_target_must_be_a_new_directory_under_an_existing_parent"
        )
    path.mkdir(mode=0o700)
    if os.name == "nt":
        try:
            result = subprocess.run(
                ["whoami", "/user", "/fo", "csv", "/nh"],
                capture_output=True,
                text=True,
                timeout=10,
                check=True,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            sid = list(csv.reader(io.StringIO(result.stdout)))[0][1]
            if not re.fullmatch(r"S-1-(?:\d+-)+\d+", sid):
                raise ValueError
            subprocess.run(
                [
                    "icacls",
                    str(path),
                    "/inheritance:r",
                    "/grant:r",
                    "*" + sid + ":(OI)(CI)F",
                    "*S-1-5-18:(OI)(CI)F",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
                check=True,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        except Exception:
            raise BackupError("backup_private_windows_acl_failed") from None
    else:
        path.chmod(0o700)
        info = path.stat()
        if info.st_mode & 0o077 or info.st_uid != os.getuid():
            raise BackupError("backup_private_permissions_failed")
    return path


def _write_json(path, value):
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, allow_nan=False
    ).encode("utf-8")
    if len(encoded) > 16 * MIB:
        raise BackupError("backup_manifest_too_large")
    with path.open("xb") as handle:
        if os.name != "nt":
            os.fchmod(handle.fileno(), 0o600)
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())


def _digest(path):
    digest = hashlib.sha256()
    with _regular(path).open("rb") as source:
        for block in iter(lambda: source.read(MIB), b""):
            digest.update(block)
    return digest.hexdigest()


def _open_sqlite_readonly(path):
    db = sqlite3.connect(
        _regular(path, max_bytes=256 * MIB).as_uri() + "?mode=ro", uri=True, timeout=5
    )
    db.execute("PRAGMA trusted_schema=OFF")
    db.execute("PRAGMA query_only=ON")
    return db


def _check_db(db, *, published=False):
    objects = dict(
        db.execute("SELECT name,type FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'")
    )
    from .credit_admission import sqlite_revision as admission_revision

    admission = admission_revision(db, published=published)
    tables = _tables(payment_revision=admission or PAYMENT_REVISION)
    allowed_names = set(tables) | (
        set() if published else AUTH_TABLES | {"alembic_version"}
    )
    # Indexes are generated by the trusted schema below; reject user triggers/views.
    actual_tables = {name for name, kind in objects.items() if kind == "table"}
    from .identity_schema import IdentitySchemaError, validate_sqlite

    try:
        identity_present = validate_sqlite(db)
    except IdentitySchemaError as error:
        raise BackupError(str(error)) from None
    from .credit_schema import CreditSchemaError, validate_sqlite as credit_sqlite

    try:
        credits_present = credit_sqlite(db)
    except CreditSchemaError as error:
        raise BackupError(str(error)) from None
    from .payment_schema import (
        PaymentSchemaError,
        sqlite_revision as payment_sqlite_revision,
    )

    try:
        payment_revision = payment_sqlite_revision(db, published=published)
        payments_present = payment_revision is not None
    except PaymentSchemaError as error:
        raise BackupError(str(error)) from None
    if admission and (
        not identity_present
        or not credits_present
        or payment_revision != LIVE_PAYMENT_REVISION
    ):
        raise BackupError("credit_admission_schema_partial_family_requires_review")
    if (credits_present or payments_present) and not identity_present:
        raise BackupError('identity_schema_partial_family_requires_review')
    if payments_present and not credits_present:
        raise BackupError('credit_schema_partial_family_requires_review')
    if actual_tables - allowed_names or any(
        kind in ("trigger", "view") for kind in objects.values()
    ):
        raise BackupError("backup_database_schema_requires_review")
    if not {"platform_jobs", "platform_assets", "platform_artifacts"}.issubset(
        actual_tables
    ):
        raise BackupError("backup_is_not_platform_database")
    if "alembic_version" in actual_tables:
        from .schema_version import (
            CREDIT_REVISIONS,
            PAYMENT_REVISIONS,
            IDENTITY_REVISIONS,
            SchemaVersionError,
            read_sqlite_schema_version,
        )

        try:
            revision = read_sqlite_schema_version(db)
        except SchemaVersionError:
            raise BackupError("backup_schema_revision_requires_review") from None
        if revision in IDENTITY_REVISIONS and not identity_present:
            raise BackupError("identity_schema_partial_family_requires_review")
        if revision in CREDIT_REVISIONS and not credits_present:
            raise BackupError('credit_schema_partial_family_requires_review')
        if revision in PAYMENT_REVISIONS and not payments_present:
            raise BackupError('payment_schema_partial_family_requires_review')
    for name in actual_tables & set(tables):
        columns = [row[1] for row in db.execute('PRAGMA table_info("' + name + '")')]
        if columns != [column.name for column in tables[name].columns]:
            raise BackupError("backup_database_version_mismatch")
    if db.execute("PRAGMA quick_check").fetchone() != ("ok",):
        raise BackupError("backup_database_integrity_failed")
    return actual_tables


def _snapshot(source_path, *, timeout_s=60):
    source = _open_sqlite_readonly(source_path)
    memory = sqlite3.connect(":memory:")
    memory.execute("PRAGMA temp_store=MEMORY")
    deadline = time.monotonic() + timeout_s
    try:

        def progress(status, remaining, total):
            if (
                time.monotonic() > deadline
                or total * source.execute("PRAGMA page_size").fetchone()[0] > 256 * MIB
            ):
                raise BackupError("backup_snapshot_size_or_time_limit")

        source.backup(memory, pages=128, progress=progress, sleep=0.01)
        _check_db(memory)
        return memory
    except Exception:
        memory.close()
        raise
    finally:
        source.close()


def _create_business_schema(
    destination,
    *,
    include_identity=False,
    include_credits=False,
    include_payments=False,
    payment_revision=PAYMENT_REVISION,
):
    # Called only for a new private output/restoration file. Never migrates a
    # source DB; creates missing current-version tables with safe empty defaults.
    engine = create_engine("sqlite:///" + destination.as_posix(), hide_parameters=True)
    try:
        with engine.begin() as connection:
            for schema in _schemas(
                include_identity=include_identity,
                include_credits=include_credits,
                include_payments=include_payments,
                payment_revision=payment_revision,
            ):
                schema.create_all(connection)
    finally:
        engine.dispose()
    if os.name != "nt":
        destination.chmod(0o600)


def _export_business_db(snapshot, destination):
    # Build trusted fresh tables, then copy only their explicit columns. The
    # source auth pages never reach any output file (not even a temporary DB).
    from .identity_schema import present_tables
    from .credit_schema import present_tables as credit_present
    from .payment_schema import present_tables as payment_present, sqlite_revision

    existing = _check_db(snapshot)
    identity_present = present_tables(existing)
    credits_present = credit_present(existing)
    payments_present = payment_present(existing)
    from .credit_admission import sqlite_revision as admission_revision

    payment_revision = (
        admission_revision(snapshot) or sqlite_revision(snapshot) or PAYMENT_REVISION
    )
    _create_business_schema(
        destination,
        include_identity=identity_present,
        include_credits=credits_present,
        include_payments=payments_present,
        payment_revision=payment_revision,
    )
    target = sqlite3.connect(destination)
    counts = {}
    try:
        for name, table in _tables(
            include_identity=identity_present,
            include_credits=credits_present,
            include_payments=payments_present,
            payment_revision=payment_revision,
        ).items():
            if name not in existing:
                counts[name] = 0
                continue
            columns = ",".join('"' + c.name + '"' for c in table.columns)
            select_sql = 'SELECT ' + columns + ' FROM "' + name + '"'  # noqa: S608 -- identifiers from fixed schema metadata, not user input
            insert_sql = (
                'INSERT INTO "'  # noqa: S608 -- identifiers from fixed schema metadata, not user input
                + name
                + '" ('
                + columns
                + ') VALUES ('
                + ','.join('?' for _ in table.columns)
                + ')'
            )
            count = 0
            cursor = snapshot.execute(select_sql)
            while batch := cursor.fetchmany(200):
                target.executemany(insert_sql, batch)
                count += len(batch)
            counts[name] = count
        target.commit()
        if target.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise BackupError("backup_database_reference_integrity_failed")
        _check_db(target, published=True)
    finally:
        target.close()
    return counts


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
            for receipt_id, receipt_tenant, receipt_owner, record_raw in rows:
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


def backup_local(
    database, object_root, destination, *, max_media_bytes=40 * 1024 * MIB
):
    """Explicit read-only source; creates one NEW private backup directory."""
    database, object_root, destination = (
        _absolute(database),
        _absolute(object_root),
        _absolute(destination),
    )
    if type(max_media_bytes) is not int or max_media_bytes < 1:
        raise BackupError("backup_media_limit_invalid")
    if not (object_root / "objects").is_dir() or not (object_root / "staging").is_dir():
        raise BackupError("backup_local_object_root_missing")
    if destination.is_relative_to(object_root) or destination == database.parent:
        raise BackupError("backup_destination_overlaps_live_storage")
    snapshot = _snapshot(database)
    try:
        references, pending = _media_references(
            snapshot, {_store_binding(LocalObjectStore(object_root), "local"): "local"}
        )
        total = sum(item["size_bytes"] for item in references.values())
        if len(references) > 100000 or total > max_media_bytes:
            raise BackupError("backup_media_limit_exceeded")
        output = _private_new_directory(destination)
        counts = _export_business_db(snapshot, output / "database.sqlite3")
        return _publish_media_backup(
            output, object_root, references, pending, counts, source_database="sqlite"
        )
    except BackupError:
        raise
    except Exception:
        raise BackupError("backup_failed_incomplete_directory_kept") from None
    finally:
        snapshot.close()


def _export_postgres_business_db(engine, destination, *, schema="public", timeout_s=60):
    """One read-only PG snapshot; copy ONLY trusted business columns to SQLite."""
    if engine.dialect.name != "postgresql" or not re.fullmatch(
        r"[A-Za-z_][A-Za-z0-9_]{0,62}", schema
    ):
        raise BackupError("backup_postgres_source_invalid")
    if type(timeout_s) is not int or not 1 <= timeout_s <= 600:
        raise BackupError("backup_postgres_timeout_invalid")
    trusted = _tables()
    deadline, byte_total, counts = time.monotonic() + timeout_s, 0, {}
    target = create_engine(
        "sqlite:///" + destination.as_posix(), echo=False, hide_parameters=True
    )
    try:
        with (
            engine.connect().execution_options(
                isolation_level="REPEATABLE READ"
            ) as source,
            source.begin(),
        ):
            source.exec_driver_sql("SET TRANSACTION READ ONLY")
            source.exec_driver_sql(
                "SET LOCAL statement_timeout = " + str(timeout_s * 1000)
            )
            source.exec_driver_sql("SET LOCAL lock_timeout = 5000")
            source.exec_driver_sql(
                "SET LOCAL idle_in_transaction_session_timeout = "
                + str(timeout_s * 1000)
            )
            inventory = {
                name: kind
                for name, kind in source.execute(
                    text(
                        "SELECT table_name,table_type FROM information_schema.tables WHERE table_schema=:schema"
                    ),
                    {"schema": schema},
                )
            }
            require_names = {"platform_jobs", "platform_assets", "platform_artifacts"}
            if (
                not require_names <= set(inventory)
                or set(inventory) - set(trusted) - AUTH_TABLES - {"alembic_version"}
                or any(kind != "BASE TABLE" for kind in inventory.values())
            ):
                raise BackupError("backup_database_schema_requires_review")
            revision = None
            if "alembic_version" in inventory:
                from .schema_version import SchemaVersionError, read_schema_version

                try:
                    revision = read_schema_version(source, schema=schema)
                except SchemaVersionError:
                    raise BackupError(
                        "backup_schema_revision_requires_review"
                    ) from None
            from .credit_admission import ready as admission_ready
            from .credits import CreditError

            try:
                admission_ready(source, schema=schema)
            except CreditError as error:
                raise BackupError(error.code) from None
            from .identity_schema import IdentitySchemaError, validate_connection

            try:
                identity_present = validate_connection(source, schema=schema)
            except IdentitySchemaError as error:
                raise BackupError(str(error)) from None
            from .credit_schema import (
                CreditSchemaError,
                validate_connection as credit_valid,
            )
            from .payment_schema import (
                PaymentSchemaError,
                validate_connection as payment_valid,
            )
            from .schema_version import (
                CREDIT_REVISIONS,
                PAYMENT_REVISIONS,
                IDENTITY_REVISIONS,
            )

            try:
                credits_present = credit_valid(source, schema=schema)
            except CreditSchemaError as error:
                raise BackupError(str(error)) from None
            try:
                payments_present = payment_valid(source, schema=schema)
                payment_revision = (
                    revision if revision in PAYMENT_REVISIONS else PAYMENT_REVISION
                )
            except PaymentSchemaError as error:
                raise BackupError(str(error)) from None

            if (
                revision in IDENTITY_REVISIONS or credits_present or payments_present
            ) and not identity_present:
                raise BackupError("identity_schema_partial_family_requires_review")
            if (
                revision in CREDIT_REVISIONS or payments_present
            ) and not credits_present:
                raise BackupError('credit_schema_partial_family_requires_review')
            if revision in PAYMENT_REVISIONS and not payments_present:
                raise BackupError('payment_schema_partial_family_requires_review')
            _create_business_schema(
                destination,
                include_identity=identity_present,
                include_credits=credits_present,
                include_payments=payments_present,
                payment_revision=payment_revision or PAYMENT_REVISION,
            )
            trusted = _tables(
                include_identity=identity_present,
                include_credits=credits_present,
                include_payments=payments_present,
                payment_revision=payment_revision or PAYMENT_REVISION,
            )
            columns = {}
            for name, column in source.execute(
                text(
                    "SELECT table_name,column_name FROM information_schema.columns WHERE table_schema=:schema ORDER BY table_name,ordinal_position"
                ),
                {"schema": schema},
            ):
                columns.setdefault(name, []).append(column)
            with target.begin() as output:
                for name, table in trusted.items():
                    if name not in inventory:
                        counts[name] = 0
                        continue
                    actual_columns = columns.get(name, [])
                    expected_columns = [column.name for column in table.columns]
                    # Explicit columns tolerate historical physical ordering.
                    if len(actual_columns) != len(expected_columns) or set(
                        actual_columns
                    ) != set(expected_columns):
                        raise BackupError("backup_database_version_mismatch")
                    relation = '"' + schema + '"."' + name + '"'
                    # Inventory fixed the read-only snapshot; bound rows before fetching.
                    count, size, largest = source.execute(
                        text(
                            "SELECT count(*),COALESCE(sum(octet_length(row_to_json(t)::text)),0),COALESCE(max(octet_length(row_to_json(t)::text)),0) FROM "  # noqa: S608 -- identifiers from fixed schema metadata, not user input
                            + relation
                            + " t"
                        )
                    ).one()
                    byte_total += int(size)
                    if (
                        count > 1000000
                        or largest > 16 * MIB
                        or byte_total > 256 * MIB
                        or time.monotonic() > deadline
                    ):
                        raise BackupError("backup_snapshot_size_or_time_limit")
                    fields = ",".join(
                        '"' + column.name + '"' for column in table.columns
                    )
                    json_nulls = {
                        column.name: "__sixnine_backup_sql_null_" + str(index)
                        for index, column in enumerate(table.columns)
                        if isinstance(column.type, JSON)
                    }
                    markers = ",".join(
                        '"' + name + '" IS NULL AS "' + marker + '"'
                        for name, marker in json_nulls.items()
                    )
                    query_fields = fields + (("," + markers) if markers else "")
                    batch_rows = min(200, max(1, 4 * MIB // max(1, int(largest))))
                    insert_sql = (
                        'INSERT INTO "'  # noqa: S608 -- identifiers from fixed schema metadata, not user input
                        + name
                        + '" ('
                        + fields
                        + ') VALUES ('
                        + ','.join('?' for _ in table.columns)
                        + ')'
                    )
                    actual = 0
                    with source.execute(
                        text(
                            "SELECT " + query_fields + " FROM " + relation  # noqa: S608 -- identifiers from fixed schema metadata, not user input
                        ).execution_options(
                            stream_results=True, max_row_buffer=batch_rows
                        )
                    ) as rows:
                        while batch := rows.mappings().fetchmany(batch_rows):
                            if time.monotonic() > deadline:
                                raise BackupError("backup_snapshot_size_or_time_limit")
                            # Markers distinguish JSON null from SQL NULL (both None).
                            values = [
                                tuple(
                                    json.dumps(
                                        row[column.name],
                                        ensure_ascii=False,
                                        allow_nan=False,
                                    )
                                    if column.name in json_nulls
                                    and not row[json_nulls[column.name]]
                                    else row[column.name]
                                    for column in table.columns
                                )
                                for row in batch
                            ]
                            output.exec_driver_sql(insert_sql, values)
                            actual += len(batch)
                            del values, batch  # Release before the next fetch.
                    if actual != count:
                        raise BackupError("backup_snapshot_count_mismatch")
                    counts[name] = actual
            from .workflow_schema import schema_exists
            from .schema_version import WORKFLOW_REVISIONS

            workflow_present = schema_exists(source)
            if workflow_present or revision in WORKFLOW_REVISIONS:
                if (
                    schema != 'public'
                    or revision not in WORKFLOW_REVISIONS
                    or not workflow_present
                ):
                    raise BackupError('backup_workflow_revision_requires_review')
                from .workflow_backup import export

                export(source, destination.parent, deadline=deadline)
            if destination.stat().st_size > 256 * MIB:
                raise BackupError("backup_snapshot_size_or_time_limit")
        with closing(_open_sqlite_readonly(destination)) as db:
            _check_db(db, published=True)
            if db.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise BackupError("backup_database_reference_integrity_failed")
        return counts
    except BackupError:
        raise
    except Exception:
        raise BackupError("backup_postgres_snapshot_failed_no_details_logged") from None
    finally:
        target.dispose()


def backup_postgres_engine(
    engine,
    object_root,
    destination,
    *,
    schema="public",
    max_media_bytes=40 * 1024 * MIB,
    stores=None,
    allow_missing_media=False,
    classify_expected_deletions=False,
    hosted_users=None,
):
    """Complete PG business + Local media backup; restore target is SQLite.

    The caller supplies an approved in-process engine; never serialize/log it.
    No pg_dump, administrative role or source schema mutation is needed.
    """
    if (
        type(allow_missing_media) is not bool
        or type(classify_expected_deletions) is not bool
        or classify_expected_deletions
        and not allow_missing_media
    ):
        raise BackupError("backup_opt_in_invalid")
    destination = _absolute(destination)
    object_root = _absolute(object_root) if object_root is not None else None
    if stores is None and (
        object_root is None
        or not (object_root / "objects").is_dir()
        or not (object_root / "staging").is_dir()
        or destination.is_relative_to(object_root)
    ):
        raise BackupError("backup_local_object_root_missing_or_overlapping")
    if stores is not None and (
        not isinstance(stores, dict) or not stores or set(stores) - {"local", "r2"}
    ):
        raise BackupError("backup_source_provider_not_configured")
    if (
        stores is not None
        and "local" in stores
        and destination.is_relative_to(_absolute(stores["local"].root))
    ):
        raise BackupError("backup_destination_overlaps_live_storage")
    if type(max_media_bytes) is not int or max_media_bytes < 1:
        raise BackupError("backup_media_limit_invalid")
    output = _private_new_directory(destination)
    try:
        counts = _export_postgres_business_db(
            engine, output / "database.sqlite3", schema=schema
        )
        with closing(_open_sqlite_readonly(output / "database.sqlite3")) as db:
            references, pending = _media_references(
                db,
                {
                    _store_binding(store, provider): provider
                    for provider, store in (
                        stores or {"local": LocalObjectStore(object_root)}
                    ).items()
                },
            )
        if (
            len(references) > 100000
            or sum(item["size_bytes"] for item in references.values()) > max_media_bytes
        ):
            raise BackupError("backup_media_limit_exceeded")
        result = _publish_media_backup(
            output,
            object_root,
            references,
            pending,
            counts,
            source_database="postgresql",
            stores=stores,
            allow_missing_media=allow_missing_media,
            classify_expected_deletions=classify_expected_deletions,
            hosted_users=hosted_users,
        )
        return {
            **result,
            "source_database": "postgresql",
            "restoration_target": "isolated-sqlite-and-local-objects",
            "native_postgres_restore": False,
        }
    except BackupError:
        raise
    except Exception:
        raise BackupError(
            "backup_postgres_local_failed_incomplete_directory_kept"
        ) from None


def backup_configured(
    destination,
    *,
    settings=None,
    max_media_bytes=2 * 1024 * MIB,
    allow_missing_media=False,
    classify_expected_deletions=False,
):
    """Use the existing app's explicit DB/storage identity, never AWS defaults.

    Runs inside the existing isolated application container. No schema creation,
    app startup or provider writes; optional hosted metadata excludes all tokens.
    """
    from .settings import Settings
    from .storage import S3ObjectStore
    from .storage_config import S3StorageConfig, load_configured_storage_credentials

    if (
        type(allow_missing_media) is not bool
        or type(classify_expected_deletions) is not bool
        or classify_expected_deletions
        and not allow_missing_media
    ):
        raise BackupError("backup_opt_in_invalid")
    settings = settings or Settings.from_environment()
    hosted_users = None
    if getattr(settings, 'workos_config_file', None) is not None:
        from .workos_backup import snapshot
        from .workos_config import load_credentials

        try:
            hosted_users = snapshot(load_credentials(settings.workos_config_file))
        except ValueError:
            hosted_users = dict(
                version=1,
                provider='workos',
                client_id='configuration_unavailable',
                observed_at=time.time(),
                status='unavailable',
                users=[],
            )
    stores = {}
    local = settings.data_dir / "objects"
    if (local / "objects").is_dir() and (local / "staging").is_dir():
        stores["local"] = LocalObjectStore(local)
    if settings.storage_provider == "r2":
        cfg = S3StorageConfig(
            "r2",
            settings.storage_endpoint,
            settings.storage_region,
            settings.storage_bucket,
            "cloudflare-r2",
            settings.storage_profile,
            enabled=True,
        )
        credentials = load_configured_storage_credentials(
            cfg,
            credentials_file=settings.storage_credential_file,
            registry_root=os.environ.get("AI_REGISTRY_ROOT"),
        )
        stores["r2"] = S3ObjectStore(cfg, credentials)
    elif settings.storage_provider != "local":
        raise BackupError("backup_source_provider_not_configured")
    engine = create_engine(
        settings.database_url,
        echo=False,
        hide_parameters=True,
        connect_args={
            "connect_timeout": 10,
            "application_name": "inkseq-business-backup",
        },
    )
    try:
        return backup_postgres_engine(
            engine,
            None,
            destination,
            stores=stores,
            max_media_bytes=max_media_bytes,
            allow_missing_media=allow_missing_media,
            classify_expected_deletions=classify_expected_deletions,
            hosted_users=hosted_users,
        )
    finally:
        engine.dispose()


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


def verify_local(directory, *, allow_incomplete=False):
    directory = _absolute(directory)
    try:
        raw = _regular(directory / "manifest.json", max_bytes=16 * MIB).read_bytes()
        manifest = json.loads(raw)
        if (
            type(allow_incomplete) is not bool
            or type(manifest.get("version")) is not int
            or manifest["version"] not in (1, 2, 3)
            or manifest.get("kind") != "sixnine-local-business-backup"
        ):
            raise BackupError("backup_format_not_supported")
        missing = manifest.get("missing_objects", [])
        if (
            not isinstance(missing, list)
            or len(missing) > 100000
            or manifest["version"] == 1
            and ("missing_objects" in manifest or "coverage" in manifest)
            or manifest["version"] in (2, 3)
            and not {"missing_objects", "coverage"} <= set(manifest)
        ):
            raise BackupError("backup_manifest_reference_mismatch")
        if missing and not allow_incomplete:
            raise BackupError("backup_incomplete_requires_explicit_opt_in")
        if 'workflow_state' in manifest:
            from .workflow_backup import verify

            verify(directory, manifest['workflow_state'])
        elif (directory / 'workflow-state.json').exists():
            raise BackupError('backup_workflow_state_unreferenced')
        if 'hosted_users' in manifest:
            from .workos_backup import read

            read(directory, manifest['hosted_users'])
        elif (directory / 'workos-users.json').exists():
            raise BackupError('backup_hosted_users_unreferenced')
        database = _regular(directory / "database.sqlite3", max_bytes=256 * MIB)
        if _digest(database) != manifest["database_sha256"]:
            raise BackupError("backup_database_checksum_mismatch")
        with closing(_open_sqlite_readonly(database)) as db:
            existing = _check_db(db, published=True)
            if db.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise BackupError("backup_database_reference_integrity_failed")
            bindings = {
                item["storage_binding"]: item.get("provider")
                for item in manifest["objects"] + missing
                if "storage_binding" in item
            }
            if any(
                not isinstance(k, str)
                or not re.fullmatch(r"[0-9a-f]{64}", k)
                or v != "r2"
                for k, v in bindings.items()
            ):
                raise BackupError("backup_manifest_reference_mismatch")
            references, pending = _media_references(db, bindings or None, offline=True)
            evidence = {}
            if manifest['version'] == 3:
                from .backup_deletion import deletion_evidence

                evidence = deletion_evidence(db, references)
            # An older trusted backup may precede newly added optional tables.
            # Verify its original table set, then create safe empty additions
            # only inside the NEW restoration target.
            counts = {
                name: db.execute('SELECT count(*) FROM "' + name + '"').fetchone()[0]  # noqa: S608 -- identifiers from fixed schema metadata, not user input
                for name in _tables()
                if name in existing
            }
            if counts != manifest.get("tables"):
                raise BackupError("backup_database_counts_mismatch")
        objects = manifest["objects"]
        if (
            not isinstance(objects, list)
            or len(objects) + len(missing) != len(references)
            or len(objects) + len(missing) > 100000
        ):
            raise BackupError("backup_manifest_reference_mismatch")
        seen = set()
        for index, item in enumerate(objects + missing):
            key = item["key"]
            if key in seen or key not in references:
                raise BackupError("backup_manifest_reference_mismatch")
            seen.add(key)
            expected = references[key]
            if any(item.get(k) != value for k, value in expected.items()):
                raise BackupError("backup_manifest_reference_mismatch")
            if index >= len(objects):
                if (
                    set(item)
                    != set(expected)
                    | {"reason", "observation", "observed_at"}
                    | (
                        {"classification", "deletion_evidence"}
                        if manifest["version"] == 3
                        else set()
                    )
                    or item["reason"] != "source_object_not_found"
                    or item["observation"]
                    != {'r2': 'head', 'local': 'local_absent'}.get(
                        item.get('provider', 'local')
                    )
                    or type(item["observed_at"]) not in (int, float)
                    or not math.isfinite(item["observed_at"])
                    or item["observed_at"] <= 0
                    or type(manifest.get("created_at")) not in (int, float)
                    or not math.isfinite(manifest["created_at"])
                    or item["observed_at"] > manifest["created_at"]
                ):
                    raise BackupError("backup_manifest_reference_mismatch")
                if manifest['version'] == 3 and (
                    not valid_classification(item, evidence)
                    or item['deletion_evidence'] != evidence.get(key, [])
                    or any(
                        p['completed_at'] > item['observed_at']
                        for p in item['deletion_evidence']
                    )
                ):
                    raise BackupError("backup_deletion_evidence_mismatch")
                continue
            if manifest["version"] in (2, 3) and set(item) != set(expected) | {"file"}:
                raise BackupError("backup_manifest_reference_mismatch")
            filename = hashlib.sha256(key.encode("ascii")).hexdigest() + ".bin"
            if item.get("file") != filename:
                raise BackupError("backup_manifest_path_invalid")
            path = _regular(directory / "media" / filename, max_bytes=512 * MIB)
            if (
                path.stat().st_size != item["size_bytes"]
                or _digest(path) != item["sha256"]
            ):
                raise BackupError("backup_media_checksum_mismatch")
        if (
            sum(item["size_bytes"] for item in objects) != manifest["media_bytes"]
            or pending != manifest["pending_assets"]
        ):
            raise BackupError("backup_manifest_totals_mismatch")
        if manifest["version"] in (2, 3):
            c = manifest["coverage"]
            if (
                not isinstance(c, dict)
                or any(
                    type(c.get(k)) is not int
                    for k in _coverage(
                        objects, missing, classified=manifest["version"] == 3
                    )
                    if k not in {"status", "loss_alert_required"}
                )
                or c != _coverage(objects, missing, classified=manifest["version"] == 3)
                or manifest["version"] == 3
                and type(c.get("loss_alert_required")) is not bool
            ):
                raise BackupError("backup_manifest_totals_mismatch")
            if {p.name for p in (directory / "media").iterdir()} != {
                item["file"] for item in objects
            }:
                raise BackupError("backup_manifest_reference_mismatch")
        return manifest
    except BackupError:
        raise
    except Exception:
        raise BackupError("backup_verification_failed") from None


def _quarantine_restored_database(path):
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("PRAGMA trusted_schema=OFF")
        held = []
        for ident, status, raw in db.execute(
            "SELECT id,status,execution_plan FROM platform_jobs"
        ).fetchall():
            execution = json.loads(raw)
            execution["enabled"] = False
            if status not in {"succeeded", "failed", "cancelled"}:
                held.append({"job_id": ident, "previous_status": status})
                db.execute(
                    "UPDATE platform_jobs SET status='recovery_hold',error_code='disaster_recovery_review_required',fence=fence+1,lease_worker_id=NULL,lease_expires_at=NULL WHERE id=?",
                    (ident,),
                )
            db.execute(
                "UPDATE platform_jobs SET execution_plan=? WHERE id=?",
                (json.dumps(execution), ident),
            )
        # Plans cannot silently re-enable old execution after account re-provisioning.
        for ident, raw in db.execute(
            "SELECT id,execution_plan FROM platform_plans"
        ).fetchall():
            execution = json.loads(raw)
            execution["enabled"] = False
            db.execute(
                "UPDATE platform_plans SET execution_plan=?,expires_at=0 WHERE id=?",
                (json.dumps(execution), ident),
            )
        db.execute(
            "UPDATE platform_registered_workers SET state='unknown',drain_requested=1,fence=fence+1,expires_at=0 WHERE state!='retired'"
        )
        db.execute(
            "UPDATE platform_registered_devices SET state='unknown' WHERE state!='released'"
        )
        db.execute("UPDATE platform_cpu_slots SET state='unknown'")
        db.execute(
            "UPDATE platform_capacity_gate SET max_instances=0,max_physical_gpus=0"
        )
        db.execute(
            "UPDATE platform_pool_limits SET max_instances=0,max_physical_gpus=0"
        )
        db.execute("UPDATE platform_scaler_leaders SET expires_at=0,fence=fence+1")
        existing = {
            row[0]
            for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if "platform_quick_chat_objects" in existing:
            for ident, kind, raw in db.execute(
                "SELECT id,kind,payload FROM platform_quick_chat_objects"
            ).fetchall():
                value = json.loads(raw)
                if kind == "turn" and value.get("status") in {"pending", "running"}:
                    value.update(status="unknown", error_code="assistant_call_unknown")
                    run = value.get("assistant_run", {})
                    run.update(
                        status="unknown",
                        error_code="assistant_call_unknown",
                        fence=run.get("fence", 0) + 1,
                    )
                    value["assistant_run"] = run
                elif kind == "execution" and value.get("status") not in {
                    "succeeded",
                    "failed",
                    "cancelled",
                }:
                    value.update(
                        status="recovery_hold",
                        error_code="disaster_recovery_review_required",
                    )
                elif kind == "preflight":
                    value.update(
                        status="blocked",
                        expires_at=0,
                        recovery_reason="disaster_recovery_review_required",
                    )
                else:
                    continue
                db.execute(
                    "UPDATE platform_quick_chat_objects SET payload=? WHERE id=?",
                    (json.dumps(value), ident),
                )
        if "platform_capacity_approvals" in existing:
            db.execute("UPDATE platform_capacity_approvals SET enabled=0")
        if "platform_capacity_waiters" in existing:
            db.execute("UPDATE platform_capacity_waiters SET state='recovery_hold'")
        # Keep provider IDs/cost reservations and original instance state as evidence.
        # Zero capacity and disabled execution are not claims external billing stopped.
        return held


def restore_local(backup_directory, destination, *, allow_incomplete=False):
    """Restore only to a NEW directory; never accepts or overwrites a live DB."""
    source, destination = _absolute(backup_directory), _absolute(destination)
    if destination.is_relative_to(source):
        raise BackupError("restore_destination_overlaps_backup")
    manifest = verify_local(source, allow_incomplete=allow_incomplete)
    target = _private_new_directory(destination)
    try:
        with (
            (source / "database.sqlite3").open("rb") as incoming,
            (target / "platform.sqlite3").open("xb") as output,
        ):
            if os.name != "nt":
                os.fchmod(output.fileno(), 0o600)
            _copy(incoming, output, 256 * MIB, manifest["database_sha256"])
            output.flush()
            os.fsync(output.fileno())
        store = LocalObjectStore(target / "objects")
        for item in manifest["objects"]:
            with _regular(source / "media" / item["file"]).open("rb") as body:
                store.put(
                    item["key"],
                    body,
                    content_type=item["content_type"],
                    max_bytes=item["size_bytes"],
                    expected_sha256=item["sha256"],
                )
        _create_business_schema(target / "platform.sqlite3")
        _restore_local_provider_metadata(target / "platform.sqlite3", manifest)
        held = _quarantine_restored_database(target / "platform.sqlite3")
        if 'hosted_users' in manifest:
            # Metadata is retained for manual provider portability. It never
            # creates credentials, sessions or upstream accounts on restore.
            with (
                (source / 'workos-users.json').open('rb') as incoming,
                (target / 'workos-users.json').open('xb') as output,
            ):
                if os.name != 'nt':
                    os.fchmod(output.fileno(), 0o600)
                _copy(incoming, output, 16 * MIB, manifest['hosted_users']['sha256'])
                output.flush()
                os.fsync(output.fileno())
        if 'workflow_state' in manifest:
            # Retain exact serialized SDK evidence; an isolated PostgreSQL restore
            # applies it only through workflow_backup.restore under its hold.
            with (
                (source / 'workflow-state.json').open('rb') as incoming,
                (target / 'workflow-state.json').open('xb') as output,
            ):
                if os.name != 'nt':
                    os.fchmod(output.fileno(), 0o600)
                _copy(incoming, output, 256 * MIB, manifest['workflow_state']['sha256'])
                output.flush()
                os.fsync(output.fileno())
        hold = dict(
            version=1,
            execution_enabled=False,
            authentication_reprovision_required=True,
            held_jobs=held,
            staging_restored=False,
            capacity_approvals_enabled=False,
            capacity_waiters_held=True,
            cloud_resources_may_still_bill=True,
        )
        if 'workflow_state' in manifest:
            hold['workflow_state'] = manifest['workflow_state']
            hold['workflow_restore'] = 'isolated_postgresql_only_requires_domain_review'
        if 'hosted_users' in manifest:
            hold['hosted_users'] = manifest['hosted_users']
        if manifest["version"] in (2, 3):
            hold.update(
                coverage=manifest["coverage"],
                missing_objects=manifest["missing_objects"],
                media_recovery_complete=not manifest["missing_objects"],
            )
        _write_json(target / "RECOVERY-HOLD.json", hold)
        _sync_directory(target)
        result = {
            "state": "restored_isolated",
            "objects": len(manifest["objects"]),
            "held_jobs": len(held),
            "authentication": "must_reprovision",
            "execution_enabled": False,
        }
        if manifest["version"] in (2, 3):
            result.update(
                coverage=manifest["coverage"],
                media_recovery_complete=not manifest["missing_objects"],
            )
        return result
    except Exception:
        raise BackupError("restore_failed_new_directory_kept_for_review") from None


def _restore_local_provider_metadata(path, manifest):
    """Rewrite only verified media locators in the new isolated recovery copy.

    The original DB/manifest retain source-provider evidence. Provider-specific
    object tokens cannot describe a newly created Local object.
    """
    remote = {
        item["key"]
        for item in manifest["objects"]
        if item.get("provider", "local") == "r2"
    }
    if not remote:
        return

    def convert(info):
        if (
            info.get("key", info.get("object_key")) in remote
            and info.get("provider", "r2") == "r2"
        ):
            info["provider"] = "local"
            for name in ("etag", "version_id"):
                if name in info:
                    info[name] = None

    with closing(sqlite3.connect(path)) as db, db:
        for ident, raw in db.execute(
            "SELECT id,record FROM platform_assets"
        ).fetchall():
            value = json.loads(raw)
            for role in ASSET_OBJECT_ROLES:
                if value.get(role):
                    convert(value[role])
            db.execute(
                "UPDATE platform_assets SET record=? WHERE id=?",
                (json.dumps(value), ident),
            )
        for ident, raw in db.execute(
            "SELECT id,metadata FROM platform_artifacts"
        ).fetchall():
            value = json.loads(raw)
            convert(value)
            for item in value.get('display_renditions', {}).get('objects', {}).values():
                convert(item)
            db.execute(
                "UPDATE platform_artifacts SET metadata=? WHERE id=?",
                (json.dumps(value), ident),
            )


def _postgres_environment(url_file, *, private_platform_network=False):
    """Private process-only connection values; never print or serialize them."""
    secret = _regular(url_file, max_bytes=16384)
    try:
        with secret.open("rb") as handle:
            info = os.fstat(handle.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or os.name != "nt"
                and info.st_mode & (stat.S_IRWXO | stat.S_IWGRP)
            ):
                raise ValueError
            raw = handle.read(16385)
            if len(raw) > 16384:
                raise ValueError
        value = raw.decode("utf-8").removesuffix("\n").removesuffix("\r")
        parsed = urlsplit(value.replace("postgresql+psycopg://", "postgresql://", 1))
        if (
            parsed.scheme != "postgresql"
            or not all((parsed.hostname, parsed.port, parsed.username, parsed.password))
            or not re.fullmatch(r"/[A-Za-z_][A-Za-z0-9_]{0,62}", parsed.path)
            or parsed.fragment
            or any(c in value for c in ("\n", "\r", "\x00"))
            or value != value.strip()
            or not re.fullmatch(r"[A-Za-z0-9.:-]+", parsed.hostname)
        ):
            raise ValueError
        options = parse_qs(parsed.query, strict_parsing=True)
        if set(options) - {"sslmode", "sslrootcert"} or any(
            len(values) != 1 for values in options.values()
        ):
            raise ValueError
        sslmode = options.get("sslmode", [""])[0]
        if type(private_platform_network) is not bool:
            raise ValueError
        if private_platform_network:
            # Operator opt-in for this repository's exact private Compose DB.
            # Never silently infer that an arbitrary remote hostname is safe.
            if (
                parsed.hostname,
                parsed.port,
                unquote(parsed.username),
                parsed.path,
            ) != ("db", 5432, "sixnine_app", "/sixnine") or parsed.query:
                raise ValueError
            sslmode = "disable"
        if not sslmode:
            if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
                raise ValueError
            sslmode = "disable"
        if sslmode not in {"disable", "verify-full"}:
            raise ValueError
        # An explicit URL query is not proof of a private network. Only the
        # reviewed Compose opt-in above or literal loopback may use plaintext;
        # otherwise a copied DSN could silently send the password off-host.
        if (
            sslmode == "disable"
            and not private_platform_network
            and parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        ):
            raise ValueError
        env = {
            name: os.environ[name]
            for name in ("PATH", "SYSTEMROOT", "TEMP", "TMP")
            if name in os.environ
        }
        env.update(
            PGHOST=parsed.hostname,
            PGPORT=str(parsed.port),
            PGUSER=unquote(parsed.username),
            PGPASSWORD=unquote(parsed.password),
            PGDATABASE=parsed.path[1:],
            PGSSLMODE=sslmode,
            PGCONNECT_TIMEOUT="10",
            PGAPPNAME="sixnine-backup",
        )
        if "sslrootcert" in options:
            env["PGSSLROOTCERT"] = str(_regular(options["sslrootcert"][0]))
    except Exception:
        raise BackupError("backup_database_url_invalid_or_not_explicit") from None
    return env


def backup_postgres_local(
    url_file,
    object_root,
    destination,
    *,
    schema="public",
    max_media_bytes=40 * 1024 * MIB,
    private_platform_network=False,
):
    """Explicit protected DSN file; no admin rights, pg_dump, or source writes."""
    env = _postgres_environment(
        url_file, private_platform_network=private_platform_network
    )
    query = {"sslmode": env["PGSSLMODE"]}
    if "PGSSLROOTCERT" in env:
        query["sslrootcert"] = env["PGSSLROOTCERT"]
    url = URL.create(
        "postgresql+psycopg",
        username=env["PGUSER"],
        password=env["PGPASSWORD"],
        host=env["PGHOST"],
        port=int(env["PGPORT"]),
        database=env["PGDATABASE"],
        query=query,
    )
    engine = create_engine(
        url,
        echo=False,
        hide_parameters=True,
        connect_args={"connect_timeout": 10, "application_name": "sixnine-backup"},
    )
    try:
        return backup_postgres_engine(
            engine,
            object_root,
            destination,
            schema=schema,
            max_media_bytes=max_media_bytes,
        )
    finally:
        engine.dispose()


def dump_postgres(
    url_file,
    destination,
    *,
    pg_dump="pg_dump",
    schema="public",
    timeout_s=600,
    include_workflows=False,
    private_platform_network=False,
):
    """Database-only explicit tool, not a verified full media backup/PG restore.

    Password stays in the child environment; no DSN/password is placed in argv,
    output, logs, or backup. Do not invoke except for an authorized DB operation.
    """
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,62}", schema):
        raise BackupError("backup_schema_invalid")
    if type(include_workflows) is not bool:
        raise BackupError('backup_opt_in_invalid')
    env = _postgres_environment(
        url_file, private_platform_network=private_platform_network
    )
    output = _private_new_directory(destination)
    command = [
        str(pg_dump),
        "--format=custom",
        "--no-owner",
        "--no-acl",
        "--no-password",
        "--no-blobs",
        "--strict-names",
        "--file=" + str(output / "database.dump"),
    ]

    def run_dump():
        subprocess.run(
            command,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout_s,
            check=True,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )

    workflow_state = None
    engine = None
    try:
        query = {'sslmode': env['PGSSLMODE']}
        if 'PGSSLROOTCERT' in env:
            query['sslrootcert'] = env['PGSSLROOTCERT']
        engine = create_engine(
            URL.create(
                'postgresql+psycopg',
                username=env['PGUSER'],
                password=env['PGPASSWORD'],
                host=env['PGHOST'],
                port=int(env['PGPORT']),
                database=env['PGDATABASE'],
                query=query,
            ),
            hide_parameters=True,
        )
        with (
            engine.connect().execution_options(
                isolation_level='REPEATABLE READ'
            ) as connection,
            connection.begin(),
        ):
            connection.exec_driver_sql('SET TRANSACTION READ ONLY')
            connection.exec_driver_sql("SET LOCAL statement_timeout = '600s'")
            connection.exec_driver_sql(
                "SET LOCAL idle_in_transaction_session_timeout = '600s'"
            )
            from .schema_version import (
                CREDIT_REVISIONS,
                PAYMENT_REVISIONS,
                IDENTITY_REVISIONS,
                WORKFLOW_REVISIONS,
                read_schema_version,
            )
            from .identity_schema import validate_connection
            from .credit_schema import validate_connection as credit_valid
            from .payment_schema import validate_connection as payment_valid

            revision = read_schema_version(connection, schema=schema)
            if revision in CREDIT_ADMISSION_REVISIONS:
                from .credit_admission import ready as admission_ready
                from .credits import CreditError

                try:
                    admission_ready(connection, schema=schema)
                except CreditError as error:
                    raise BackupError(error.code) from None
            identity_present = validate_connection(connection, schema=schema)
            credits_present = credit_valid(connection, schema=schema)
            payments_present = payment_valid(connection, schema=schema)
            if (
                revision in IDENTITY_REVISIONS or credits_present or payments_present
            ) and not identity_present:
                raise BackupError('identity_schema_partial_family_requires_review')
            if (
                revision in CREDIT_REVISIONS or payments_present
            ) and not credits_present:
                raise BackupError('credit_schema_partial_family_requires_review')
            if revision in PAYMENT_REVISIONS and not payments_present:
                raise BackupError('payment_schema_partial_family_requires_review')
            command += [
                "--table=" + schema + "." + name
                for name in sorted(
                    _tables(
                        include_identity=identity_present,
                        include_credits=credits_present,
                        include_payments=payments_present,
                    )
                )
            ]
            if include_workflows:
                from .workflow_schema import schema_exists

                present = schema_exists(connection)
                if present or revision in WORKFLOW_REVISIONS:
                    if (
                        schema != 'public'
                        or revision not in WORKFLOW_REVISIONS
                        or not present
                    ):
                        raise BackupError('backup_workflow_revision_requires_review')
                    from .workflow_backup import export

                    workflow_state = export(connection, output)
            snapshot = connection.exec_driver_sql(
                'SELECT pg_export_snapshot()'
            ).scalar_one()
            if not re.fullmatch(r'[A-Za-z0-9-]{1,100}', snapshot):
                raise BackupError('backup_snapshot_identifier_invalid')
            command.append('--snapshot=' + snapshot)
            run_dump()
        archive = _regular(output / "database.dump")
        if not archive.stat().st_size:
            raise BackupError("backup_postgres_dump_empty")
        if os.name != "nt":
            archive.chmod(0o600)
        manifest = dict(
            version=1,
            kind="postgres-database-only",
            database_sha256=_digest(archive),
            media_included=False,
            restore_verified=False,
            authentication_included=False,
            created_at=time.time(),
        )
        if workflow_state:
            manifest['workflow_state'] = workflow_state
            manifest['consistent_business_workflow_snapshot'] = True
        _write_json(output / "manifest.json", manifest)
        return {
            "state": "postgres_database_dumped",
            "media_included": False,
            "restore_verified": False,
        }
    except Exception:
        raise BackupError("backup_postgres_dump_failed_no_secret_output") from None
    finally:
        if engine is not None:
            engine.dispose()
