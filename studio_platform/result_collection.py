"""Completion-time document projection; caller owns the owner fence/transaction.

No network, schema creation, generation, or financial operations. Legacy managed
Quick Create projections remain immutable; their existing take API is authoritative.
"""

from __future__ import annotations

import copy
from datetime import datetime, timezone

from sqlalchemy import select, update

from .content_lifecycle import assert_project_writable
from .project_validation import validate_project
from .repository import Conflict, documents, jobs
from .source_snapshot import source_snapshot
from .universal_trash import assert_authoring_available


def contract():
    return {
        "version": 1,
        "receipt": "job.result.project_collection",
        "fields": [
            "version",
            "status",
            "entity_ids",
            "project_version",
            "selected_entity_id",
        ],
        "statuses": [
            "bound",
            "unchanged",
            "not_applicable",
            "missing_project",
            "missing_shot",
            "managed_projection",
            "project_unavailable",
            "document_rejected",
        ],
        "read": "Refresh GET /v1/projects/{project_id} after bound; use its current version for actions. Do not PUT collected results on open/poll. No writes or historical backfill occur on GET.",
        "selection": "Only a real, fresh first take with no existing selection/range/segments and no later accepted job may be selected. Existing choices are never replaced. Stale/simulated outputs remain candidates.",
        "legacy": "Absent receipt means historical completion; versions and explicit artifact.adopt remain supported. managed_projection uses the existing Quick Create take API, never a project PUT.",
    }


def _receipt(status, **extra):
    return {"version": 1, "status": status, "entity_ids": [], **extra}


def _entity(job, ident, spec, now, *, stale, order):
    kind = spec["kind"]
    ref = job["request"]["client_ref"]
    # Public projection is closed: never persist storage keys/provider evidence.
    metadata = {
        key: copy.deepcopy(spec[key])
        for key in (
            "kind",
            "size_bytes",
            "sha256",
            "content_type",
            "width",
            "height",
            "duration_s",
            "fps",
            "has_audio",
            "quality_flags",
            "quality_checks",
        )
        if key in spec
    }
    return {
        "id": "result-" + ident,
        "type": kind,
        "title": "Generated result",
        "description": "",
        "parentId": None,
        "order": order,
        "version": 1,
        "status": "draft",
        "data": {
            "fileId": "cloud_artifact_" + ident,
            "cloudArtifactId": ident,
            "cloudContentPath": f"/v1/artifacts/{ident}/content",
            "fileName": ident
            + {"video": ".mp4", "image": ".png", "audio": ".flac"}[kind],
            "mime": spec.get(
                "content_type",
                {"video": "video/mp4", "image": "image/png", "audio": "audio/flac"}[
                    kind
                ],
            ),
            "bytes": spec["size_bytes"],
            "metadata": metadata,
            "simulation": _simulation(job),
            "source": "generation",
            "sourceJobId": job["id"],
            "sourceShotId": ref["shot_id"],
            "sourceShotVersion": ref.get("shot_version"),
            "sourceHash": ref.get("source_hash"),
            "oldVersion": stale,
            "missingFile": False,
            "createdAt": now,
        },
    }


def _simulation(job):
    return (
        job["execution_plan"].get("backend") == "mock"
        or job["request"].get("simulation") is True
    )


def collect(repo, conn, job, artifact_ids, specs):
    """Return a durable closed receipt; unexpected DB failures roll back completion.

    Expected authoring refusals do not lose a paid result: the artifact/settlement
    commit with a skipped receipt, and the existing explicit adoption API remains.
    """
    ref = job["request"].get("client_ref")
    if (
        not isinstance(ref, dict)
        or ref.get("project_id") != job["project_id"]
        or not isinstance(ref.get("shot_id"), str)
        or job["request"].get("recipe_id") == "chapter-roughcut-v1"
    ):
        return _receipt("not_applicable")
    media = [
        (ident, spec)
        for ident, spec in zip(artifact_ids, specs, strict=True)
        if spec["kind"] in {"video", "image", "audio"}
    ]
    if not media:
        return _receipt("not_applicable")
    where = (
        documents.c.tenant_id == job["tenant_id"],
        documents.c.owner_id == job["owner_id"],
        documents.c.project_id == "__projects",
        documents.c.kind == "project",
        documents.c.document_id == job["project_id"],
    )
    row = repo._locked(conn, select(documents).where(*where))
    if row is None or row["payload"].get("content_deleted"):
        return _receipt("missing_project")
    project = copy.deepcopy(row["payload"])
    if (
        project.get("integration_kind") == "quick_chat"
        or project.get("journey", {}).get("integration_kind") == "quick_chat"
    ):
        return _receipt("managed_projection")
    try:
        assert_project_writable(
            conn, job["tenant_id"], job["owner_id"], job["project_id"]
        )
        assert_authoring_available(conn, job["tenant_id"], job["owner_id"], project)
    except Conflict:
        return _receipt("project_unavailable")
    try:
        validate_project(project)
    except ValueError:
        return _receipt("document_rejected")
    shot = next(
        (
            e
            for e in project["entities"]
            if e["id"] == ref["shot_id"] and e["type"] in {"shot", "generation"}
        ),
        None,
    )
    if shot is None:
        return _receipt("missing_shot")
    original = copy.deepcopy(project)
    fresh = bool(job["request"].get("server_source_hash")) and job["request"][
        "server_source_hash"
    ] == source_snapshot(project, shot["id"])
    later = conn.execute(
        select(jobs.c.id)
        .where(
            jobs.c.tenant_id == job["tenant_id"],
            jobs.c.owner_id == job["owner_id"],
            jobs.c.project_id == job["project_id"],
            jobs.c.id != job["id"],
            jobs.c.request["client_ref"]["shot_id"].as_string() == shot["id"],
            jobs.c.created_at >= job["created_at"],
        )
        .limit(1)
    ).first()
    data = shot["data"]
    may_select = (
        fresh
        and not later
        and not _simulation(job)
        and job["status"] != "cancel_requested"
        and shot["type"] == "shot"
        and not data.get("selectedAssetId")
        and not data.get("selectedVideoRange")
        and not data.get("videoSegments")
    )
    now = (
        datetime.fromtimestamp(repo.clock(), timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )
    bound, selected = [], None
    for ident, spec in media:
        found = [
            e for e in project["entities"] if e["data"].get("cloudArtifactId") == ident
        ]
        if len(found) > 1:
            return _receipt("document_rejected")
        entity = found[0] if found else None
        if entity is not None and entity["type"] != spec["kind"]:
            return _receipt("document_rejected")
        if entity is None:
            if any(e["id"] == "result-" + ident for e in project["entities"]):
                return _receipt("document_rejected")
            entity = _entity(
                job,
                ident,
                spec,
                now,
                stale=not fresh,
                order=sum(e["parentId"] is None for e in project["entities"]),
            )
            project["entities"].append(entity)
        bound.append(entity["id"])
        if spec["kind"] in {"video", "image"}:
            candidates = data.setdefault("candidateIds", [])
            if entity["id"] not in candidates:
                candidates.append(entity["id"])
            if may_select and selected is None:
                data["selectedAssetId"] = entity["id"]
                selected = entity["id"]
    if project == original:
        return _receipt("unchanged", entity_ids=bound, project_version=row["version"])
    if shot["data"] != next(
        e["data"] for e in original["entities"] if e["id"] == shot["id"]
    ):
        shot["version"] += 1
    project["updatedAt"] = now
    try:
        validate_project(project)
        assert_authoring_available(conn, job["tenant_id"], job["owner_id"], project)
    except (ValueError, Conflict):
        return _receipt("document_rejected")
    version = row["version"] + 1
    changed = conn.execute(
        update(documents)
        .where(*where, documents.c.version == row["version"])
        .values(payload=project, version=version, updated_at=repo.clock())
    )
    if changed.rowcount != 1:
        raise Conflict("document_version_conflict")
    receipt = _receipt("bound", entity_ids=bound, project_version=version)
    if selected:
        receipt["selected_entity_id"] = selected
    repo._emit(
        conn,
        "project.results_collected",
        job["project_id"],
        {
            "tenant_id": job["tenant_id"],
            "owner_id": job["owner_id"],
            "job_id": job["id"],
            **receipt,
        },
    )
    return receipt
