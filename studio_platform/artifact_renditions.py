"""Auxiliary display writes on the original ArtifactWriter receipt, never GET.

GPU delivery roles and their sealed manifest remain video/audio only. A bounded
prefix is captured during full original SHA validation; only one frame is
decoded. No original video is staged or decoded in full on the CPU R2 path.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from pathlib import Path

from sqlalchemy import insert, select, update

from . import media, scratch_retention
from .media_renditions import (
    RENDITIONS,
    POSTER_PREFIX_BYTES,
    POSTER_FRAME_BYTES,
    create_video_renditions,
)
from .repository import artifacts
from .storage import (
    IntegrityError,
    ObjectNotFound,
    ObjectAlreadyExists,
    StorageWriteUncertain,
    _check_ancestors,
    _no_links,
    _part,
    validate_key,
)
from .storage_asset_journal import artifact_accounting, artifact_identity


def physical_objects(value):
    """Published auxiliary physical objects; originals remain separate."""
    display = value.get("display_renditions", {})
    objects = display.get('objects', {}) if isinstance(display, dict) else {}
    return list(objects.values()) if isinstance(objects, dict) else []


def _data_root(value):
    if (
        not isinstance(value, str)
        or not value
        or str(Path(value)) != value
        or not Path(value).is_absolute()
        or '..' in Path(value).parts
    ):
        raise IntegrityError('artifact_poster_scratch_root_invalid')
    root = Path(value)
    _check_ancestors(root)
    if not root.is_dir():
        raise IntegrityError('artifact_poster_scratch_root_invalid')
    return root


def _directory(writer, record):
    display = record.get('display', {})
    if 'scratch_data_dir' not in display:
        return writer.work_dir / _part(record['tag']) / 'display-v1'
    return (
        _data_root(display['scratch_data_dir'])
        / 'artifact-display'
        / _part(writer.tenant)
        / _part(record['owner'])
        / _part(record['id'])
        / 'display-v1'
    )


def _file(writer, record, name):
    if name not in {
        "poster-source.mp4",
        "poster-frame.png",
        "thumb-v1.webp",
        "preview-v1.webp",
    }:
        raise IntegrityError("artifact_poster_path_invalid")
    path = _directory(writer, record) / name
    _check_ancestors(path)
    if path.exists() or path.is_symlink():
        info = _no_links(path)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise IntegrityError("artifact_poster_file_invalid")
    return path


def _validate(writer, record):
    display = record.get('display')
    if not display:
        return
    if (
        not isinstance(display, dict)
        or type(display.get('version')) is not int
        or display['version'] != 1
        or display.get('phase') not in {'planned', 'prepared', 'complete', 'missing'}
        or display.get('source_sha256') != record['roles']['video']['sha256']
        or type(display.get('reserved_bytes')) is not int
        or display['reserved_bytes'] < 0
        or not isinstance(display.get('objects'), dict)
        or set(display['objects']) not in (set(), set(RENDITIONS))
    ):
        raise IntegrityError('artifact_poster_receipt_invalid')
    if 'scratch_data_dir' in display:
        directory = _directory(writer, record)
        _check_ancestors(directory)
        if directory.exists():
            if not directory.is_dir():
                raise IntegrityError('artifact_poster_file_invalid')
            for path in directory.iterdir():
                _file(writer, record, path.name)
    for name, role in display['objects'].items():
        edge, cap = RENDITIONS[name]
        if (
            role.get('filename') != f'{name}-v1.webp'
            or role.get('key')
            != f'owners/{record["owner"]}/assets/{record["tag"]}/display-{name}-v1.webp'
            or role.get('status') not in {'planned', 'putting', 'unknown', 'verified'}
            or role.get('content_type') != 'image/webp'
            or type(role.get('size_bytes')) is not int
            or not 1 <= role['size_bytes'] <= min(cap, writer.max_bytes)
            or not isinstance(role.get('sha256'), str)
            or re.fullmatch('[0-9a-f]{64}', role['sha256']) is None
            or any(
                type(role.get(k)) is not int or not 1 <= role[k] <= edge
                for k in ('width', 'height')
            )
        ):
            raise IntegrityError('artifact_poster_receipt_invalid')


def _scratch(writer, record):
    directory = _file(writer, record, "poster-source.mp4").parent
    if not directory.exists():
        return {}
    paths = [_file(writer, record, path.name) for path in directory.iterdir()]
    return scratch_retention.manifest(
        directory, paths, max(writer.max_bytes, POSTER_FRAME_BYTES)
    )


def _resize_reservation(writer, record, size):
    display = record["display"]
    previous = display.get("reserved_bytes", 0)
    record["reserved_bytes"] += size - previous
    display["reserved_bytes"] = size
    try:
        writer._save_display(record, byte_delta=size - previous)
    except BaseException:
        record["reserved_bytes"] -= size - previous
        display["reserved_bytes"] = previous
        raise


def _publish(writer, record, *, status):
    """Receipt CAS, original artifact metadata and accounting commit together."""
    from .artifact_writer import write_receipts, ArtifactWritePending

    display = record["display"]
    with writer.engine.begin() as conn:
        writer._display_update(conn, record)
        row = (
            conn.execute(
                select(write_receipts)
                .where(write_receipts.c.id == record["id"])
                .with_for_update()
            )
            .mappings()
            .one()
        )
        if row["version"] != record["version"]:
            raise ArtifactWritePending("artifact_poster_state_changed")
        rows = [
            item
            for item in conn.execute(
                select(artifacts).where(
                    artifacts.c.job_id == row["job_id"],
                    artifacts.c.attempt_id == row["attempt_id"],
                )
            ).mappings()
            if item["metadata"].get("kind") == "video"
        ]
        if (
            len(rows) != 1
            or any(
                rows[0]["metadata"].get(k) != record["roles"]["video"].get(k)
                for k in ("sha256", "size_bytes", "content_type")
            )
            or rows[0]['metadata'].get('object_key') != record['roles']['video']['key']
        ):
            raise IntegrityError("artifact_poster_original_changed")
        value = dict(rows[0]["metadata"])
        if status == "ready":
            if any(
                role.get("status") != "verified" for role in display["objects"].values()
            ):
                raise IntegrityError("artifact_poster_not_verified")
            for role in display["objects"].values():
                ident, size, sha = artifact_identity(
                    dict(role, object_key=role["key"], validated=True), record["owner"]
                )
                for scope, _, _ in writer.quota._scopes(record["owner"]):
                    marker = (
                        conn.execute(
                            select(artifact_accounting).where(
                                artifact_accounting.c.scope == scope,
                                artifact_accounting.c.object_id == ident,
                            )
                        )
                        .mappings()
                        .first()
                    )
                    if marker is None:
                        conn.execute(
                            insert(artifact_accounting).values(
                                scope=scope,
                                object_id=ident,
                                size_bytes=size,
                                sha256=sha,
                            )
                        )
                    elif (marker["size_bytes"], marker["sha256"]) != (size, sha):
                        raise IntegrityError("artifact_poster_accounting_conflict")
            value["display_renditions"] = dict(
                metadata=dict(
                    version=1,
                    source_sha256=display["source_sha256"],
                    **{
                        key: {name: role[name] for name in ("width", "height")}
                        for key, role in display["objects"].items()
                    },
                ),
                objects={
                    key: {
                        name: role[name]
                        for name in ("key", "size_bytes", "sha256", "content_type")
                    }
                    for key, role in display["objects"].items()
                },
            )
            value["rendition_status"] = "ready"
            display["phase"] = "complete"
            display.pop("error_code", None)
        elif status == "missing":
            value["rendition_status"] = "missing"
            display["phase"] = "missing"
        elif status == "pending":
            value["rendition_status"] = "pending"
        else:
            raise IntegrityError("artifact_poster_publication_state_invalid")
        conn.execute(
            update(artifacts)
            .where(artifacts.c.id == rows[0]["id"])
            .values(metadata=value)
        )
        data = {key: value for key, value in record.items() if key != "version"}
        changed = conn.execute(
            update(write_receipts)
            .where(
                write_receipts.c.id == record["id"],
                write_receipts.c.version == record["version"],
            )
            .values(version=record["version"] + 1, record=json.dumps(data))
        )
        if changed.rowcount != 1:
            raise ArtifactWritePending("artifact_poster_state_changed")
    record["version"] += 1


def cleanup(writer, record):
    """Only frozen display scratch, after verified publication; crash-repeatable."""
    display = record.get("display", {})
    _validate(writer, record)
    if display.get("phase") != "complete" and not (
        display.get("phase") == "missing" and not display.get('objects')
    ):
        return
    manifest = display.get("scratch_cleanup")
    if not manifest or manifest["phase"] == "complete":
        return
    for role in display["objects"].values():
        writer._verify_remote(role, lambda: None)
    remote = sum(role["size_bytes"] for role in display["objects"].values())
    if display["reserved_bytes"] != remote + manifest["bytes"]:
        raise IntegrityError("artifact_poster_scratch_accounting_mismatch")
    directory = _file(writer, record, "poster-source.mp4").parent
    scratch_retention.remove_manifest(
        directory, manifest["files"], max(writer.max_bytes, POSTER_FRAME_BYTES)
    )
    manifest["phase"] = "complete"
    _resize_reservation(writer, record, remote)
    if directory.exists():
        directory.rmdir()
    if directory.parent.exists() and not any(directory.parent.iterdir()):
        directory.parent.rmdir()


def ensure(
    writer,
    job,
    attempt_id,
    tag,
    *,
    expected_version=None,
    interrupted=False,
    heartbeat=lambda: None,
    display_data_dir=None,
):
    """Called after accepted collection or explicit exact-version offline apply.

    An unresolved old display writer is never taken over based on age. The
    caller must explicitly assert stopped writers AND the original version.
    """
    from .artifact_writer import ArtifactWritePending

    record = writer.get(job, attempt_id, tag)
    if record is None or record["phase"] != "settled" or "video" not in record["roles"]:
        raise ArtifactWritePending("artifact_poster_original_unconfirmed")
    if expected_version is not None and (
        type(expected_version) is not int or record["version"] != expected_version
    ):
        raise ArtifactWritePending("artifact_poster_version_changed")
    if interrupted and expected_version is None:
        raise ArtifactWritePending("artifact_poster_exact_version_required")
    requested_root = None
    if display_data_dir is not None:
        if expected_version is None:
            raise ArtifactWritePending('artifact_poster_exact_version_required')
        requested_root = str(_data_root(str(display_data_dir)))
    existing = record.get("display")
    if existing and existing.get('scratch_data_dir') != requested_root:
        raise ArtifactWritePending('artifact_poster_scratch_root_changed')
    _validate(writer, record)
    if existing and existing.get("phase") == "complete":
        cleanup(writer, record)
        return record
    if existing and existing.get('phase') == 'missing':
        cleanup(writer, record)
    if existing and existing.get("phase") not in {"missing"} and not interrupted:
        raise ArtifactWritePending("artifact_poster_writer_unconfirmed")
    source = record["roles"]["video"]
    if source.get("status") != "verified":
        raise ArtifactWritePending("artifact_poster_original_unconfirmed")
    writer.quota._ensure(record["owner"])
    display = record.setdefault(
        "display",
        dict(
            version=1,
            phase="planned",
            source_sha256=source["sha256"],
            objects={},
            reserved_bytes=0,
        ),
    )
    if existing is None and requested_root is not None:
        display['scratch_data_dir'] = requested_root
        directory = _directory(writer, record)
        _check_ancestors(directory)
        # A fresh auxiliary intent cannot adopt unexplained old scratch.
        if directory.exists() and (not directory.is_dir() or any(directory.iterdir())):
            raise IntegrityError('artifact_poster_scratch_unconfirmed')
    if display.get("source_sha256") != source["sha256"] or display.get("version") != 1:
        raise IntegrityError("artifact_poster_source_changed")
    if display['phase'] == 'missing':
        # cleanup above completed the previous failed extraction's exact
        # manifest. Persist a fresh active claim before any reservation/IO;
        # another retry must not treat this writer as a quiescent failure.
        display['phase'] = 'planned'
        display.pop('scratch_cleanup', None)
        display.pop('error_code', None)
    # Claim the original receipt and publish pending in the same transaction,
    # before reservation, download or decode can begin.
    _publish(writer, record, status="pending")
    if not display["objects"]:
        _resize_reservation(
            writer,
            record,
            max(
                display["reserved_bytes"],
                POSTER_PREFIX_BYTES
                + POSTER_FRAME_BYTES
                + 2 * sum(cap for _, cap in RENDITIONS.values()),
            ),
        )
        directory = _file(writer, record, "poster-source.mp4").parent
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        _check_ancestors(directory)
        # Only retain a bounded prefix. Validate the complete original SHA even
        # when it was not already verified by this writer's collection pass.
        prefix = getattr(writer, "_poster_prefix", {}).get(
            (source["key"], source["sha256"])
        )
        if prefix is None:
            import io

            buffer = io.BytesIO()
            writer._verify_remote(source, heartbeat, prefix=buffer)
            prefix = buffer.getvalue()
        if not isinstance(prefix, bytes) or len(prefix) > POSTER_PREFIX_BYTES:
            raise IntegrityError('artifact_poster_prefix_invalid')
        path = _file(writer, record, "poster-source.mp4")
        with path.open("wb") as target:
            target.write(prefix)
            target.flush()
            os.fsync(target.fileno())
        try:
            for name in ("poster-frame.png", "thumb-v1.webp", "preview-v1.webp"):
                _file(
                    writer, record, name
                )  # Refuse links before a deterministic unpublished replacement.
            with media.PROCESSING_ADMISSION.acquire("video"):
                roles = create_video_renditions(
                    path, directory, max_bytes=writer.max_bytes
                )
        except media.MediaError:
            display["error_code"] = "poster_first_frame_unavailable_within_bounds"
            manifest = _scratch(writer, record)
            display["scratch_cleanup"] = dict(
                phase="pending",
                files=manifest,
                bytes=sum(v["size_bytes"] for v in manifest.values()),
            )
            _resize_reservation(writer, record, display["scratch_cleanup"]["bytes"])
            _publish(writer, record, status="missing")
            cleanup(writer, record)
            return record
        for name, role in roles.items():
            role.update(
                key=validate_key(
                    f'owners/{record["owner"]}/assets/{tag}/display-{name}-v1.webp'
                ),
                status="planned",
            )
        display.update(objects=roles, phase="prepared")
        manifest = _scratch(writer, record)
        display["scratch_cleanup"] = dict(
            phase="pending",
            files=manifest,
            bytes=sum(v["size_bytes"] for v in manifest.values()),
        )
        _resize_reservation(
            writer,
            record,
            sum(role["size_bytes"] for role in roles.values())
            + display["scratch_cleanup"]["bytes"],
        )
    for role in display["objects"].values():
        path = _file(writer, record, role["filename"])
        if writer._digest(path) != (role["size_bytes"], role["sha256"]):
            raise IntegrityError("artifact_poster_staging_changed")
        try:
            writer._verify_remote(role, heartbeat)
        except ObjectNotFound:
            if role["status"] in {"putting", "unknown"} and not interrupted:
                raise ArtifactWritePending("artifact_poster_put_unknown") from None
            role["status"] = "putting"
            writer._save_display(record)
            try:
                with path.open("rb") as stream:
                    writer.store.put(
                        role["key"],
                        stream,
                        content_type="image/webp",
                        max_bytes=writer.max_bytes,
                        expected_sha256=role["sha256"],
                    )
            except (ObjectAlreadyExists, StorageWriteUncertain):
                role["status"] = "unknown"
                writer._save_display(record)
                raise ArtifactWritePending("artifact_poster_put_unknown") from None
            writer._verify_remote(role, heartbeat)
        role["status"] = "verified"
        writer._save_display(record)
    _publish(writer, record, status="ready")
    cleanup(writer, record)
    return record
