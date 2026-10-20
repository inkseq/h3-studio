"""Explicit immutable Local-to-R2 copy; no database, deletion or import-time IO.

Private plans bind the source files and exact target. Successful copy means all
planned objects were fully read back, not that concurrent writers were fenced or
the production storage configuration was changed. Local bytes always remain.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import time
import uuid

from .storage import (
    LocalObjectStore,
    ObjectAlreadyExists,
    ObjectNotFound,
    S3ObjectStore,
    _check_ancestors,
    _no_links,
    _sync_directory,
    validate_key,
    key_belongs_to,
)
from .storage_config import S3StorageConfig

FORMAT = "sixnine-local-r2-copy-v1"
MAX_OBJECTS = 100000
MAX_BYTES = 40 * 1024**3
MAX_JSON = 16 * 1024**2
CHUNK = 1024**2


class MigrationError(Exception):
    """Only static safe codes; no raw SDK exception, URL or credential values."""


def _need(value, code):
    if not value:
        raise MigrationError(code)


def _canonical(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


def _sha(value):
    return hashlib.sha256(value).hexdigest()


def _identity(info):
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]


def _absolute(value):
    path = Path(value)
    _need(
        path.is_absolute() and ".." not in path.parts,
        "migration_explicit_absolute_path_required",
    )
    _check_ancestors(path)
    return path


def _private(path):
    path = _absolute(path)
    info = _no_links(path)
    _need(stat.S_ISDIR(info.st_mode), "migration_private_directory_required")
    if os.name != "nt":
        _need(
            info.st_uid == os.getuid() and not info.st_mode & 0o077,
            "migration_private_directory_required",
        )
    return path


def _new_private(path):
    path = _absolute(path)
    _need(
        not path.exists() and path.parent.is_dir(),
        "migration_new_private_directory_required",
    )
    path.mkdir(mode=0o700)
    if os.name == "nt":
        # Restrict the private plan (object keys and local paths are not public).
        result = subprocess.run(
            ["whoami", "/user", "/fo", "csv", "/nh"],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        import csv
        import io

        sid = list(csv.reader(io.StringIO(result.stdout)))[0][1]
        _need(
            bool(re.fullmatch(r"S-1-(?:\d+-)+\d+", sid)), "migration_private_acl_failed"
        )
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
    return _private(path)


def _write(path, value, *, replace=False):
    parent = _private(path.parent)
    data = _canonical(value)
    _need(len(data) <= MAX_JSON, "migration_receipt_limit")
    if path.exists():
        _need(replace, "migration_receipt_already_exists")
        _read(path)
    temporary = parent / (".copy-" + uuid.uuid4().hex)
    try:
        fd = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        with os.fdopen(fd, "wb") as target:
            target.write(data)
            target.flush()
            os.fsync(target.fileno())
        if replace:
            os.replace(temporary, path)
        else:
            # Exclusive publication; never overwrite another operator's plan.
            os.link(temporary, path)
            temporary.unlink()
        _sync_directory(parent)
    finally:
        temporary.unlink(missing_ok=True)


def _read(path):
    _private(path.parent)
    with LocalObjectStore._read_file(path) as source:
        info = os.fstat(source.fileno())
        _need(
            os.name == "nt"
            or (info.st_uid == os.getuid() and not info.st_mode & 0o077),
            "migration_private_file_required",
        )
        raw = source.read(MAX_JSON + 1)
    _need(len(raw) <= MAX_JSON, "migration_receipt_limit")

    def unique(pairs):
        value = {}
        for key, item in pairs:
            _need(key not in value, "migration_receipt_duplicate_field")
            value[key] = item
        return value

    try:
        return json.loads(raw, object_pairs_hook=unique)
    except (ValueError, TypeError):
        raise MigrationError("migration_receipt_invalid") from None


@contextmanager
def _lock(directory):
    directory = _private(directory)
    path = directory / "copy.lock"
    fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "r+b") as handle:
        info = os.fstat(handle.fileno())
        _need(
            stat.S_ISREG(info.st_mode) and info.st_nlink == 1, "migration_lock_invalid"
        )
        if os.name != "nt":
            _need(
                info.st_uid == os.getuid() and not info.st_mode & 0o077,
                "migration_lock_invalid",
            )
            import fcntl

            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise MigrationError("migration_copy_already_running") from None
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)
        else:
            import msvcrt

            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                raise MigrationError("migration_copy_already_running") from None
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def _target(config):
    _need(
        isinstance(config, S3StorageConfig)
        and config.provider == "r2"
        and config.enabled,
        "migration_r2_target_required",
    )
    return asdict(config)


def _source(root):
    root = _absolute(root)
    for path in (root, root / "objects", root / "staging"):
        info = _no_links(path)
        _need(stat.S_ISDIR(info.st_mode), "migration_existing_local_store_required")
    return LocalObjectStore(root)


def _directory_names(store):
    _check_ancestors(store.root)
    _no_links(store.root / "objects")
    names = []
    for path in (store.root / "objects").iterdir():
        info = _no_links(path)
        _need(
            re.fullmatch(r"[a-f0-9]{64}", path.name) and stat.S_ISDIR(info.st_mode),
            "migration_unexpected_object_directory",
        )
        names.append(path.name)
        _need(len(names) <= MAX_OBJECTS, "migration_object_count_limit")
    return sorted(names)


def _snapshot(store, name, limit):
    directory = store.root / "objects" / name
    _no_links(directory)
    _need(
        {p.name for p in directory.iterdir()} == {"blob", "meta.json"},
        "migration_unexpected_object_files",
    )
    meta = directory / "meta.json"
    before_meta = _identity(_no_links(meta))
    with LocalObjectStore._read_file(meta) as incoming:
        raw = incoming.read(4097)
    _need(len(raw) <= 4096, "migration_object_metadata_invalid")
    try:
        key = json.loads(raw)["key"]
        validate_key(key)
    except (ValueError, TypeError, KeyError):
        raise MigrationError("migration_object_metadata_invalid") from None
    _need(store._directory(key) == directory, "migration_logical_key_mismatch")
    info = store.stat(key)
    _need(0 < info.size_bytes <= limit, "migration_object_exceeds_single_put_limit")
    blob = directory / "blob"
    before_blob = _identity(_no_links(blob))
    digest, count = hashlib.sha256(), 0
    with store.open(key) as incoming:
        opened = os.fstat(incoming.fileno())
        # Windows fstat/lstat have different ctime semantics. Compare that field
        # within each API, while stable file identity is compared across APIs.
        _need(_identity(opened)[:4] == before_blob[:4], "migration_source_changed")
        for chunk in iter(lambda: incoming.read(CHUNK), b""):
            count += len(chunk)
            _need(count <= info.size_bytes, "migration_source_size_mismatch")
            digest.update(chunk)
        _need(
            _identity(os.fstat(incoming.fileno())) == _identity(opened),
            "migration_source_changed",
        )
    _need(
        _identity(_no_links(meta)) == before_meta
        and _identity(_no_links(blob)) == before_blob,
        "migration_source_changed",
    )
    _need(
        count == info.size_bytes and digest.hexdigest() == info.sha256,
        "migration_source_hash_mismatch",
    )
    return dict(
        key=key,
        directory=name,
        size_bytes=count,
        sha256=info.sha256,
        content_type=info.content_type,
        blob_identity=before_blob,
        meta_identity=before_meta,
        meta_sha256=_sha(raw),
    )


def create_plan(source_root, target_config, receipt_directory):
    """Local-only dry plan. No client, credentials, provider or DB is accessed."""
    store = _source(source_root)
    target = _target(target_config)
    destination = _absolute(receipt_directory)
    _need(
        not destination.is_relative_to(store.root)
        and not store.root.is_relative_to(destination),
        "migration_receipt_overlaps_source",
    )
    names = _directory_names(store)
    objects, total = [], 0
    for name in names:
        item = _snapshot(store, name, target_config.max_single_put_bytes)
        objects.append(item)
        total += item["size_bytes"]
        _need(total <= MAX_BYTES, "migration_total_bytes_limit")
    _need(_directory_names(store) == names, "migration_source_inventory_changed")
    plan = dict(
        format=FORMAT,
        operation_id=uuid.uuid4().hex,
        created_at=time.time(),
        source_root=str(store.root),
        source_identity=_identity(store.root.stat())[:2],
        target=target,
        objects=objects,
        total_bytes=total,
        local_copies_retained=True,
        database_accessed=False,
        writer_fence_verified=False,
    )
    _need(len(_canonical(plan)) <= MAX_JSON, "migration_receipt_limit")
    _new_private(destination)
    _write(destination / "plan.json", plan)
    return dict(
        state="migration_planned",
        objects=len(objects),
        bytes=total,
        plan_sha256=_sha(_canonical(plan)),
        local_copies_retained=True,
        database_accessed=False,
        writer_fence_verified=False,
    )


def _load(directory, remote):
    _need(isinstance(remote, S3ObjectStore), "migration_reviewed_r2_store_required")
    directory = _private(directory)
    plan = _read(directory / "plan.json")
    _need(
        type(plan) is dict
        and plan.get("format") == FORMAT
        and plan.get("target") == _target(remote.config)
        and plan.get("local_copies_retained") is True
        and plan.get("database_accessed") is False
        and re.fullmatch(r"[0-9a-f]{32}", plan.get("operation_id", "")),
        "migration_plan_binding_invalid",
    )
    _need(
        type(plan.get("objects")) is list and len(plan["objects"]) <= MAX_OBJECTS,
        "migration_plan_objects_invalid",
    )
    store = _source(plan["source_root"])
    _need(
        _identity(store.root.stat())[:2] == plan["source_identity"],
        "migration_source_identity_changed",
    )
    names = [item["directory"] for item in plan["objects"]]
    _need(
        names == sorted(set(names)) and _directory_names(store) == names,
        "migration_source_inventory_changed",
    )
    total = 0
    for item in plan["objects"]:
        _need(
            _snapshot(store, item["directory"], remote.config.max_single_put_bytes)
            == item,
            "migration_source_changed_since_plan",
        )
        total += item["size_bytes"]
    _need(
        total == plan.get("total_bytes") and total <= MAX_BYTES,
        "migration_plan_bytes_invalid",
    )
    return plan, store


def _remote_verify(remote, item):
    info = remote.stat(item["key"])
    _need(
        (info.size_bytes, info.sha256, info.content_type)
        == (item["size_bytes"], item["sha256"], item["content_type"]),
        "migration_remote_metadata_mismatch",
    )
    digest, count = hashlib.sha256(), 0
    with remote.open(item["key"]) as source:
        while True:
            data = source.read(min(CHUNK, item["size_bytes"] - count + 1))
            if not data:
                break
            count += len(data)
            _need(count <= item["size_bytes"], "migration_remote_size_mismatch")
            digest.update(data)
    _need(
        count == item["size_bytes"] and digest.hexdigest() == item["sha256"],
        "migration_remote_hash_mismatch",
    )


def _receipt(plan):
    return dict(
        format=FORMAT,
        operation_id=plan["operation_id"],
        plan_sha256=_sha(_canonical(plan)),
        target=plan["target"],
        phase="copying",
        objects={},
        local_copies_retained=True,
        database_accessed=False,
        writer_fence_verified=False,
    )


def apply_plan(receipt_directory, remote):
    """Copy once per planned key; uncertain writes only reconcile, never repeat."""
    directory = _private(receipt_directory)
    with _lock(directory):
        plan, store = _load(directory, remote)
        path = directory / "copy-receipt.json"
        receipt = _read(path) if path.exists() else _receipt(plan)
        _need(
            receipt.get("operation_id") == plan["operation_id"]
            and receipt.get("plan_sha256") == _sha(_canonical(plan))
            and receipt.get("target") == plan["target"]
            and type(receipt.get("objects")) is dict
            and not set(receipt["objects"]) - {item["key"] for item in plan["objects"]},
            "migration_receipt_binding_invalid",
        )
        _write(path, receipt, replace=path.exists())
        for item in plan["objects"]:
            key = item["key"]
            phase = receipt["objects"].get(key, "planned")
            _need(
                phase in {"planned", "putting", "unknown", "verified"},
                "migration_receipt_phase_invalid",
            )
            try:
                _remote_verify(remote, item)
            except ObjectNotFound:
                _need(phase == "planned", "migration_unknown_write_requires_review")
                receipt["objects"][key] = "putting"
                _write(path, receipt, replace=True)
                try:
                    _need(
                        _snapshot(
                            store, item["directory"], remote.config.max_single_put_bytes
                        )
                        == item,
                        "migration_source_changed_since_plan",
                    )
                    with store.open(key) as source:
                        remote.put(
                            key,
                            source,
                            content_type=item["content_type"],
                            max_bytes=item["size_bytes"],
                            expected_sha256=item["sha256"],
                        )
                except ObjectAlreadyExists:
                    # Another immutable copy may win; adoption requires full verification.
                    pass
                except Exception:
                    receipt["objects"][key] = "unknown"
                    _write(path, receipt, replace=True)
                    raise MigrationError(
                        "migration_write_unconfirmed_receipt_retained"
                    ) from None
                _remote_verify(remote, item)
            _need(
                _snapshot(store, item["directory"], remote.config.max_single_put_bytes)
                == item,
                "migration_source_changed_since_plan",
            )
            receipt["objects"][key] = "verified"
            _write(path, receipt, replace=True)
        _need(
            _directory_names(store) == [item["directory"] for item in plan["objects"]],
            "migration_source_inventory_changed",
        )
        # Earlier copied source files can change while later objects are read.
        # Validate the complete original source snapshot again before success.
        final_plan, _ = _load(directory, remote)
        _need(final_plan == plan, "migration_plan_changed")
        receipt.update(
            phase="copy_verified",
            verified_at=time.time(),
            objects_count=len(plan["objects"]),
            total_bytes=plan["total_bytes"],
        )
        _write(path, receipt, replace=True)
        return dict(
            state="migration_copy_verified",
            objects=len(plan["objects"]),
            bytes=plan["total_bytes"],
            plan_sha256=receipt["plan_sha256"],
            local_copies_retained=True,
            database_accessed=False,
            writer_fence_verified=False,
        )


def verify_plan(receipt_directory, remote):
    """Read-only local and complete R2 GET checks; does not change receipts."""
    directory = _private(receipt_directory)
    with _lock(directory):
        plan, store = _load(directory, remote)
        for item in plan["objects"]:
            _remote_verify(remote, item)
        _need(
            _directory_names(store) == [item["directory"] for item in plan["objects"]],
            "migration_source_inventory_changed",
        )
        final_plan, _ = _load(directory, remote)
        _need(final_plan == plan, "migration_plan_changed")
        return dict(
            state="migration_readback_verified",
            objects=len(plan["objects"]),
            bytes=plan["total_bytes"],
            plan_sha256=_sha(_canonical(plan)),
            local_copies_retained=True,
            database_accessed=False,
            writer_fence_verified=False,
        )


def verify_rendition_sources(
    receipt_directory, remote, asset_receipt, *, logical_root=None, provenance_file=None
):
    return _verify_rendition_sources(
        receipt_directory,
        remote,
        asset_receipt,
        logical_root=logical_root,
        provenance_file=provenance_file,
    )


def verify_artifact_rendition_sources(
    receipt_directory,
    remote,
    artifact_receipt,
    *,
    logical_root=None,
    provenance_file=None,
):
    """Exact copied video/audio roles; ArtifactWriter uses compact identity JSON."""
    _need(
        artifact_receipt.get("phase") == "settled"
        and type(artifact_receipt.get("roles")) is dict
        and "video" in artifact_receipt["roles"]
        and set(artifact_receipt["roles"]) <= {"video", "audio"},
        "rendition_copy_original_manifest_required",
    )
    return _verify_rendition_sources(
        receipt_directory,
        remote,
        artifact_receipt,
        logical_root=logical_root,
        provenance_file=provenance_file,
        artifact=True,
    )


def _verify_rendition_sources(
    receipt_directory,
    remote,
    asset_receipt,
    *,
    logical_root,
    provenance_file,
    artifact=False,
):
    """Read only exact copied originals; never adopt a binding or scan Local.

    A historical container path needs explicit, protected accepted mount proof.
    The current R2 bytes must still match every original/model manifest in full.
    """
    _need(isinstance(remote, S3ObjectStore), "rendition_copy_r2_required")
    directory = _private(receipt_directory)

    def read_digest(path):
        value = _read(path)
        with LocalObjectStore._read_file(path) as source:
            info = os.fstat(source.fileno())
            _need(
                os.name == "nt"
                or info.st_uid == os.getuid()
                and not info.st_mode & 0o077,
                "rendition_copy_private_proof_required",
            )
            raw = source.read(MAX_JSON + 1)
        _need(
            len(raw) <= MAX_JSON and json.loads(raw) == value,
            "rendition_copy_proof_changed",
        )
        return value, _sha(raw)

    plan, raw_plan_sha = read_digest(directory / "plan.json")
    copied, copied_sha = read_digest(directory / "copy-receipt.json")
    _need(
        plan.get("format") == FORMAT
        and plan.get("target") == _target(remote.config)
        and plan.get("local_copies_retained") is True
        and plan.get("database_accessed") is False
        and copied.get("format") == FORMAT
        and copied.get("phase") == "copy_verified"
        and copied.get("operation_id") == plan.get("operation_id")
        and copied.get("plan_sha256") == _sha(_canonical(plan))
        and copied.get("target") == plan["target"]
        and copied.get("local_copies_retained") is True
        and copied.get("database_accessed") is False
        and type(plan.get("objects")) is list
        and len(plan["objects"]) <= MAX_OBJECTS,
        "rendition_copy_proof_binding_invalid",
    )
    source_root = Path(plan["source_root"])
    _need(
        source_root.is_absolute() and ".." not in source_root.parts,
        "rendition_copy_source_root_invalid",
    )
    logical = source_root if logical_root is None else Path(logical_root)
    _need(
        logical.is_absolute() and ".." not in logical.parts,
        "rendition_copy_source_root_invalid",
    )
    local_identity = {"provider": "local", "root": str(logical)}
    source_binding = _sha(
        _canonical(local_identity)
        if artifact
        else json.dumps(local_identity, sort_keys=True).encode()
    )
    _need(
        source_binding
        == asset_receipt.get("binding" if artifact else "storage_binding"),
        "rendition_copy_source_binding_mismatch",
    )
    provenance_sha = None
    if logical != source_root:
        _need(provenance_file is not None, "rendition_copy_mount_provenance_required")
        proof, provenance_sha = read_digest(Path(provenance_file))
        mount = proof.get("covering_mount", {})
        _need(
            type(proof.get("version")) is int
            and proof["version"] == 1
            and proof.get("type") == "logical_local_mount_provenance"
            and proof.get("logical_root") == str(logical)
            and proof.get("source_root") == str(source_root)
            and proof.get("source_identity") == plan.get("source_identity")
            and proof.get("source_binding") == source_binding
            and proof.get("raw_plan_sha256") == raw_plan_sha
            and proof.get("canonical_plan_sha256") == _sha(_canonical(plan))
            and proof.get("copy_receipt_sha256") == copied_sha
            and mount.get("Type") == "bind"
            and Path(mount["Source"]).is_absolute()
            and Path(mount["Destination"]).is_absolute()
            and ".." not in Path(mount["Source"]).parts
            and ".." not in Path(mount["Destination"]).parts
            and Path(mount["Source"]) / logical.relative_to(mount["Destination"])
            == source_root
            and re.fullmatch(
                "[0-9a-f]{64}", proof.get("matched_runtime_mounts_sha256", "")
            ),
            "rendition_copy_mount_provenance_invalid",
        )
        _check_ancestors(logical)
        info = _no_links(logical)
        _need(
            stat.S_ISDIR(info.st_mode)
            and _identity(info)[:2] == plan["source_identity"],
            "rendition_copy_source_identity_changed",
        )
    elif provenance_file is not None:
        raise MigrationError("rendition_copy_unexpected_mount_provenance")
    objects = {item["key"]: item for item in plan["objects"]}
    _need(
        len(objects) == len(plan["objects"])
        and type(copied.get("objects")) is dict
        and set(copied["objects"]) == set(objects)
        and all(value == "verified" for value in copied["objects"].values()),
        "rendition_copy_objects_unconfirmed",
    )
    _need(
        all(
            type(item.get("size_bytes")) is int
            and 1 <= item["size_bytes"] <= remote.config.max_single_put_bytes
            for item in objects.values()
        )
        and type(plan.get("total_bytes")) is int
        and 0 <= plan["total_bytes"] <= MAX_BYTES
        and sum(item["size_bytes"] for item in objects.values()) == plan["total_bytes"]
        and copied.get("objects_count") == len(objects)
        and copied.get("total_bytes") == plan["total_bytes"],
        "rendition_copy_bytes_invalid",
    )
    sources = {}
    original_roles = asset_receipt["roles"] if artifact else asset_receipt["asset"]
    for role in ("video", "audio") if artifact else ("original", "model"):
        spec = original_roles.get(role)
        if spec is None:
            continue
        frozen = spec if artifact else asset_receipt.get("objects", {}).get(role, {})
        item = objects.get(spec.get("key"), {})
        _need(
            key_belongs_to(spec.get("key"), asset_receipt["owner"])
            and frozen.get("status" if artifact else "phase") == "verified"
            and all(
                spec.get(key) == frozen.get(key) == item.get(key)
                for key in ("key", "size_bytes", "sha256", "content_type")
            ),
            "rendition_copy_original_manifest_mismatch",
        )
        sources[spec["key"]] = item
    _need(
        ("video" if artifact else "original") in original_roles and sources,
        "rendition_copy_original_manifest_required",
    )
    for item in sources.values():
        _remote_verify(remote, item)
    _need(
        read_digest(directory / "plan.json") == (plan, raw_plan_sha)
        and read_digest(directory / "copy-receipt.json") == (copied, copied_sha),
        "rendition_copy_proof_changed",
    )
    if provenance_sha is not None:
        _need(
            read_digest(Path(provenance_file))[1] == provenance_sha
            and _identity(_no_links(logical))[:2] == plan["source_identity"],
            "rendition_copy_proof_changed",
        )
    identity = {
        key: getattr(remote.config, key)
        for key in (
            "provider",
            "endpoint_url",
            "region",
            "bucket",
            "service",
            "profile",
        )
    }
    return dict(
        version=1,
        source_binding=source_binding,
        target_binding=_sha(
            _canonical(identity)
            if artifact
            else json.dumps(identity, sort_keys=True).encode()
        ),
        plan_sha256=raw_plan_sha,
        copy_receipt_sha256=copied_sha,
        provenance_sha256=provenance_sha,
        sources_sha256=_sha(_canonical([sources[key] for key in sorted(sources)])),
    )
