"""Operator-only legacy story adoption on existing rows; no schema or providers.

Preview is read-only. Apply is staging-only and preserves every original ID.
Receipt archives group original takes under one logical card without losing IDs.
"""

from __future__ import annotations

import copy
import json
import math
import re
import time
import uuid

from sqlalchemy import inspect, insert, or_, select, update

from .assets import asset_table
from .content_lifecycle import (
    assert_project_writable,
    assert_reference_payload,
    owner_reference_gate,
)
from .generation_draft import input_entries
from .identity_schema import users
from .project_validation import validate_project
from .quick_chat import events, objects
from .source_snapshot import source_snapshot
from .repository import (
    Conflict,
    artifacts,
    attempts,
    canonical,
    documents,
    jobs,
    registered_workers,
    request_hash,
)

KIND = "quick_chat_story_migration"
CONTRACT = 1
MAPPING_VERSION = 2
TERMINAL = {"succeeded", "failed", "cancelled"}
REQUIRED = {
    objects.name,
    events.name,
    documents.name,
    jobs.name,
    artifacts.name,
    attempts.name,
    asset_table.name,
    users.name,
}


class MigrationError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def require(value, code):
    if not value:
        raise MigrationError(code)


def _projection_matches(project, shot, item):
    if item.get("projection_hash") == source_snapshot(project, shot["id"]):
        return "original_hash"
    # Original QuickChat configure creates version2/review. Later ordinary
    # artifact adoption/selection changes only these outcome fields/version.
    # The original content hash must still match after precisely that delta.
    if (
        shot.get("status") != "review"
        or type(shot.get("version")) is not int
        or shot["version"] < 2
        or not ({"candidateIds", "selectedAssetId"} & shot["data"].keys())
    ):
        return False
    restored = copy.deepcopy(project)
    target = next(e for e in restored["entities"] if e["id"] == shot["id"])
    target["version"] = 2
    target["data"].pop("candidateIds", None)
    target["data"].pop("selectedAssetId", None)
    return (
        "outcome_only_normalized_hash"
        if item.get("projection_hash") == source_snapshot(restored, shot["id"])
        else None
    )


def _factory_placeholder(project, shot, closure):
    ident = "shot-" + uuid.uuid5(uuid.NAMESPACE_URL, project["id"] + ":shot").hex
    scene = "scene-" + uuid.uuid5(uuid.NAMESPACE_URL, project["id"] + ":scene").hex
    expected = {
        "id": ident,
        "type": "shot",
        "parentId": scene,
        "title": "我的视频",
        "description": "",
        "version": 1,
        "order": 0,
        "status": "draft",
        "data": {"seconds": 5, "prompt": ""},
    }
    return (
        shot == expected
        and not any(
            ident in (link.get("source"), link.get("target"))
            for link in project["links"]
        )
        and not any(e.get("parentId") == ident for e in project["entities"])
        and not any(
            j["request"].get("client_ref", {}).get("shot_id") == ident
            for j in closure["jobs"]
        )
    )


def _group_shots(project, session, cards, revisions, item_map, closure):
    lookup = {e["id"]: e for e in project["entities"]}
    projected = {value["shot_id"] for value in item_map.values()}
    require(len(projected) == len(item_map), "projected_shot_identity_conflict")
    unmapped = [
        e
        for e in project["entities"]
        if e["type"] == "shot" and e["id"] not in projected
    ]
    require(
        len(unmapped) <= 1
        and all(_factory_placeholder(project, e, closure) for e in unmapped),
        "unmapped_existing_shots_require_review",
    )
    scenes = {lookup[ident]["parentId"] for ident in projected} | {
        e["parentId"] for e in unmapped
    }
    require(
        len(scenes) == 1 and lookup.get(next(iter(scenes)), {}).get("type") == "scene",
        "scene_attachment_mapping_requires_review",
    )
    groups, ordered_cards = {}, []
    for card_id, card in cards.items():
        values = [r for r in revisions if r["parent_id"] == card_id]
        require(
            values
            and all(
                type(r["payload"].get("version")) is int
                and r["payload"]["version"] >= 1
                and type(r["created_at"]) in {float, int}
                and math.isfinite(r["created_at"])
                for r in values
            ),
            "revision_order_unknown",
        )
        require(
            len({r["payload"]["version"] for r in values}) == len(values),
            "revision_order_ambiguous",
        )
        by_version = sorted(values, key=lambda r: r["payload"]["version"])
        require(
            all(
                a["created_at"] <= b["created_at"]
                for a, b in zip(by_version, by_version[1:])
            ),
            "revision_clock_order_ambiguous",
        )
        latest = by_version[-1]
        require(
            card["payload"].get("current_revision_id") == latest["id"],
            "current_revision_not_latest",
        )
        source_ids = []
        for revision in by_version:
            items = revision["payload"]["items"]
            require(
                all(type(item.get("index")) is int for item in items)
                and {item["index"] for item in items} == set(range(len(items))),
                "revision_item_order_ambiguous",
            )
            source_ids.extend(
                item["shot_id"]
                for item in sorted(items, key=lambda item: item["index"])
            )
        choices = [
            ident
            for ident in source_ids
            if lookup[ident]["data"].get("selectedAssetId")
        ]
        require(len(choices) <= 1, "multiple_persisted_choices")
        first_item = next(
            item for item in latest["payload"]["items"] if item["index"] == 0
        )
        canonical_id = choices[0] if choices else first_item["shot_id"]
        groups[canonical_id] = {
            "card_id": card_id,
            "source_shot_ids": source_ids,
            "selected_asset_id": lookup[canonical_id]["data"].get("selectedAssetId")
            or None,
            "selection_reason": "persisted_selection"
            if choices
            else "current_revision_first_item",
        }
        ordered_cards.append((by_version[0]["created_at"], card_id, canonical_id))
    ordered_cards.sort()
    target = copy.deepcopy(project)
    targets = {e["id"]: e for e in target["entities"]}
    archived = {}
    for order, (_, _, canonical_id) in enumerate(ordered_cards):
        for ident in groups[canonical_id]["source_shot_ids"]:
            entity = targets[ident]
            require(
                "migration" not in entity["data"], "migration_marker_already_present"
            )
            if ident == canonical_id:
                entity["data"]["migration"] = {
                    "canonical": True,
                    "source_shot_ids": groups[canonical_id]["source_shot_ids"],
                    "source_session_id": session["id"],
                    "receipt_id": session["id"],
                }
                entity["order"] = order
                entity["version"] += 1
            else:
                item_id = next(
                    item_id
                    for item_id, value in item_map.items()
                    if value["shot_id"] == ident
                )
                archived[ident] = {
                    "entity": copy.deepcopy(lookup[ident]),
                    "projection_hash": item_map[item_id]["projection_hash"],
                    "shot_id": ident,
                    "source_session_id": session["id"],
                }
    for original in unmapped:
        archived[original["id"]] = {
            "entity": copy.deepcopy(original),
            "projection_hash": None,
            "shot_id": original["id"],
            "source_session_id": session["id"],
        }
    require(
        not any(e.get("parentId") in archived for e in project["entities"]),
        "archived_shot_has_children",
    )
    require(
        not any(link["source"] in archived for link in project["links"]),
        "archived_shot_has_outgoing_dependency",
    )

    def references_archived(value):
        if isinstance(value, str):
            return value in archived
        if isinstance(value, list):
            return any(references_archived(v) for v in value)
        if isinstance(value, dict):
            return any(
                key in archived or references_archived(v) for key, v in value.items()
            )
        return False

    require(
        not references_archived(
            {
                key: value
                for key, value in project["journey"].items()
                if key != "reviewShotId"
            }
        ),
        "archived_shot_has_authoring_reference",
    )
    job_lookup = {row["id"]: row for row in closure["jobs"]}
    artifact_lookup = {row["id"]: row for row in closure["artifacts"]}
    for entity in project["entities"]:
        if entity["id"] in archived:
            continue
        data = dict(entity["data"])
        if data.get("sourceShotId") in archived:
            job = job_lookup.get(data.get("sourceJobId"))
            artifact = artifact_lookup.get(data.get("cloudArtifactId"))
            ref = job["request"].get("client_ref", {}) if job else {}
            require(
                entity["type"] in {"video", "image", "audio"}
                and job is not None
                and artifact is not None
                and job["tenant_id"] == session["tenant"]
                and job["owner_id"] == session["owner"]
                and job["project_id"] == project["id"]
                and job["status"] == "succeeded"
                and artifact["job_id"] == job["id"]
                and artifact["metadata"].get("kind") == entity["type"]
                and not artifact["metadata"].get("content_deleted")
                and ref.get("shot_id") == data["sourceShotId"]
                and all(
                    key not in data or data[key] == ref.get(field)
                    for key, field in (
                        ("sourceShotVersion", "shot_version"),
                        ("sourceHash", "source_hash"),
                    )
                ),
                "archived_media_provenance_unverified",
            )
            # Exempt only verified immutable provenance in the scan copy.
            data.pop("sourceShotId")
        require(not references_archived(data), "archived_shot_has_authoring_reference")
    focus_before = target["journey"].get("reviewShotId")
    require(
        focus_before is None
        or isinstance(focus_before, str)
        and lookup.get(focus_before, {}).get("type") == "shot",
        "review_shot_binding_unknown",
    )
    focus_after = focus_before
    if focus_before in archived:
        focus_after = next(
            (
                canonical_id
                for canonical_id, group in groups.items()
                if focus_before in group["source_shot_ids"]
            ),
            ordered_cards[0][2] if ordered_cards else None,
        )
        if focus_after is None:
            target["journey"].pop("reviewShotId", None)
        else:
            target["journey"]["reviewShotId"] = focus_after
    archived_links = [
        copy.deepcopy(link) for link in project["links"] if link["target"] in archived
    ]
    positions = target.get("layout", {}).get("positions", {})
    archived_positions = {
        key: copy.deepcopy(value) for key, value in positions.items() if key in archived
    }
    for key in archived_positions:
        positions.pop(key)
    target["entities"] = [
        entity for entity in target["entities"] if entity["id"] not in archived
    ]
    target["links"] = [
        link for link in target["links"] if link["target"] not in archived
    ]
    return (
        target,
        groups,
        next(iter(scenes)),
        {
            "canonical_shots": len(groups),
            "retained_shots": 0,
            "archived_shots": len(archived),
            "removed_projections": len(archived),
            "aliased_takes": len(projected) - len(groups),
            "cards_with_choice": sum(
                bool(g["selected_asset_id"]) for g in groups.values()
            ),
            "cards_without_choice": sum(
                g["selected_asset_id"] is None for g in groups.values()
            ),
            "factory_placeholders": len(unmapped),
            "review_focus_remapped": int(
                focus_before != focus_after and focus_after is not None
            ),
            "review_focus_removed": int(
                focus_before != focus_after and focus_after is None
            ),
        },
        {
            "archive_strategy": "receipt_archive",
            "archived_shots": archived,
            "archived_links": archived_links,
            "archived_positions": archived_positions,
            "review_shot_navigation": {"before": focus_before, "after": focus_after},
        },
    )


def _project_where(tenant, owner, project_id):
    return (
        documents.c.tenant_id == tenant,
        documents.c.owner_id == owner,
        documents.c.project_id == "__projects",
        documents.c.kind == "project",
        documents.c.document_id == project_id,
    )


def _receipt_where(tenant, owner, project_id, session_id):
    return (
        documents.c.tenant_id == tenant,
        documents.c.owner_id == owner,
        documents.c.project_id == project_id,
        documents.c.kind == KIND,
        documents.c.document_id == session_id,
    )


def has_migration_receipt(connection, tenant, owner, session_id):
    """Any adoption receipt fences legacy writes, including malformed receipts."""
    rows = list(
        connection.execute(
            select(documents)
            .where(
                documents.c.tenant_id == tenant,
                documents.c.owner_id == owner,
                documents.c.kind == KIND,
                documents.c.document_id == session_id,
            )
            .limit(2)
        ).mappings()
    )
    return bool(rows) and (len(rows) != 1 or not _completed_rollback(rows[0]))


def _completed_rollback(row):
    value = row["payload"]
    if not isinstance(value, dict):
        return False
    rollback = value.get("rollback")
    before = value.get("before_project")
    if not isinstance(rollback, dict) or not isinstance(before, dict):
        return False
    return (
        row["version"] == 2
        and value.get("contract") == CONTRACT
        and value.get("mapping_version") == MAPPING_VERSION
        and value.get("status") == "completed"
        and value.get("archive_strategy") == "receipt_archive"
        and value.get("receipt_id")
        == row["document_id"]
        == value.get("source_session_id")
        and value.get("tenant_id") == row["tenant_id"]
        and value.get("owner_id") == row["owner_id"]
        and value.get("project_id") == row["project_id"] == before.get("id")
        and _valid_before_project(value, row["project_id"], row["document_id"])
        and rollback.get("status") == "completed"
        and isinstance(rollback.get("actor_user_id"), str)
        and isinstance(rollback.get("actor_owner"), str)
        and isinstance(rollback.get("reason"), str)
        and rollback.get("original_after_project_hash")
        == value.get("after_project_hash")
        and rollback.get("restored_project_hash") == value.get("before_project_hash")
        and type(value.get("after_project_version")) is int
        and rollback.get("before_project_version") == value["after_project_version"]
        and rollback.get("restored_project_version")
        == value["after_project_version"] + 1
    )


def assert_legacy_mutable(connection, tenant, owner, session_ids):
    for ident in session_ids:
        if has_migration_receipt(connection, tenant, owner, ident):
            raise Conflict("quick_chat_migrated_read_only")


def validate_reader_evidence(value):
    """Operator's deployed-reader self-check, not Claude migration acceptance."""
    require(
        isinstance(value, dict)
        and set(value)
        == {
            "environment",
            "platform_sha",
            "frontend_sha",
            "archive_contract",
            "story_chat_archive",
            "versions_alias",
            "works_archive",
            "self_check_id",
        },
        "deployed_readers_not_verified",
    )
    require(
        value["environment"] == "staging"
        and type(value["archive_contract"]) is int
        and value["archive_contract"] == MAPPING_VERSION
        and all(
            isinstance(value[key], str) and re.fullmatch(r"[a-f0-9]{40}", value[key])
            for key in ("platform_sha", "frontend_sha")
        )
        and all(
            type(value[key]) is int and value[key] == 1
            for key in ("story_chat_archive", "versions_alias", "works_archive")
        )
        and isinstance(value["self_check_id"], str)
        and 1 <= len(value["self_check_id"]) <= 200,
        "deployed_readers_not_verified",
    )
    return canonical(value)


def _valid_before_project(receipt, project_id, session_id):
    before = receipt.get("before_project")
    if (
        not isinstance(before, dict)
        or before.get("id") != project_id
        or before.get("schemaVersion") != 4
        or before.get("integration_kind") != "quick_chat"
        or not isinstance(before.get("journey"), dict)
        or before["journey"].get("integration_kind") != "quick_chat"
        or before["journey"].get("quickChatSessionId") != session_id
    ):
        return False
    try:
        return request_hash(before) == receipt.get("before_project_hash")
    except (TypeError, ValueError):
        return False


def _bounded_payload(payload, limit):
    size = len(
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    )
    require(size <= min(limit, 8 * 1024**2), "archive_receipt_too_large")
    return size


def _archive_payload(project, analysis):
    return {
        "before_project": project,
        "archive_strategy": "receipt_archive",
        "archived_shots": analysis["archived_shots"],
        "archived_links": analysis["archived_links"],
        "archived_positions": analysis["archived_positions"],
        "canonical_shots": analysis["canonical_shots"],
        "mapping": analysis["mapping"],
        "review_shot_navigation": analysis["review_shot_navigation"],
    }


def _source_rows_hash(closure):
    return request_hash({key: closure[key] for key in ("session", "objects", "events")})


def _operational_hash(closure):
    keys = (
        "jobs",
        "attempts",
        "artifacts",
        "assets",
        "uploads",
        "collections",
        "workers",
        "obligations",
    )
    payload = {key: closure[key] for key in keys}
    payload["references"] = [
        row
        for row in closure["references"]
        if not (
            row["project_id"] == closure["project"]["document_id"]
            and row["kind"] == KIND
            and row["document_id"] == closure["session"]["id"]
        )
    ]
    return request_hash(payload)


def _rollback_record(receipt, actor, at, reason):
    return {
        "status": "completed",
        "actor_user_id": actor["id"],
        "actor_owner": actor["owner"],
        "reason": reason,
        "before_project_version": receipt["after_project_version"],
        "restored_project_version": receipt["after_project_version"] + 1,
        "original_after_project_hash": receipt["after_project_hash"],
        "restored_project_hash": receipt["before_project_hash"],
        "completed_at": at,
    }


def migrated_project_id(connection, tenant, owner, session):
    """An owner-checked terminal receipt is authority; a payload hint is not."""
    if (
        session.get("tenant") != tenant
        or session.get("owner") != owner
        or session.get("kind") != "session"
        or session.get("session_id") != session.get("id")
    ):
        return None
    project_id = session.get("payload", {}).get("project_id")
    if not isinstance(project_id, str):
        return None
    row = (
        connection.execute(
            select(documents).where(
                *_receipt_where(tenant, owner, project_id, session["id"])
            )
        )
        .mappings()
        .first()
    )

    project = (
        connection.execute(
            select(documents).where(*_project_where(tenant, owner, project_id))
        )
        .mappings()
        .first()
    )
    if not row or not project:
        return None
    receipt = row["payload"]
    p = project["payload"]
    if (
        not isinstance(receipt, dict)
        or not isinstance(p, dict)
        or not isinstance(p.get("journey"), dict)
    ):
        return None
    journey = p["journey"]
    if not isinstance(journey.get("storyChat"), dict) or not isinstance(
        journey.get("quickChatMigration"), dict
    ):
        return None
    if (
        receipt.get("contract") != CONTRACT
        or receipt.get("archive_strategy") != "receipt_archive"
        or "rollback" in receipt
        or not _valid_before_project(receipt, project_id, session["id"])
        or receipt.get("mapping_version") != MAPPING_VERSION
        or receipt.get("receipt_id") != session["id"]
        or receipt.get("tenant_id") != tenant
        or receipt.get("owner_id") != owner
        or receipt.get("status") != "completed"
        or row["version"] != 1
        or receipt.get("source_session_id") != session["id"]
        or receipt.get("project_id") != project_id
        or p.get("id") != project_id
        or not isinstance(receipt.get("source_hash"), str)
        or not re.fullmatch(r"[a-f0-9]{64}", receipt["source_hash"])
        or type(receipt.get("after_project_version")) is not int
        or project["version"] < receipt["after_project_version"]
        or p.get("integration_kind") == "quick_chat"
        or journey.get("integration_kind") == "quick_chat"
        or journey["storyChat"].get("version") != 1
        or journey["quickChatMigration"].get("session_id") != session["id"]
        or journey["quickChatMigration"].get("contract") != CONTRACT
    ):
        return None
    return project_id


def _load(connection, tenant, owner, session_id, *, lock=False, max_rows=20000):
    require(
        REQUIRED <= set(inspect(connection).get_table_names()),
        "migration_schema_unavailable",
    )
    statement = select(objects).where(
        objects.c.tenant == tenant,
        objects.c.owner == owner,
        objects.c.id == session_id,
        objects.c.kind == "session",
        objects.c.session_id == session_id,
    )
    if lock:
        statement = statement.with_for_update()
    session = connection.execute(statement).mappings().first()
    require(session is not None, "session_not_found")
    session = dict(session)
    require(isinstance(session["payload"], dict), "source_payload_invalid")
    project_id = session["payload"].get("project_id")
    require(isinstance(project_id, str) and project_id, "project_binding_missing")
    statement = select(documents).where(*_project_where(tenant, owner, project_id))
    if lock:
        statement = statement.with_for_update()
    project = connection.execute(statement).mappings().first()
    require(project is not None, "owned_project_missing")
    closure = {"session": session, "project": dict(project)}
    queries = {
        "objects": select(objects)
        .where(
            objects.c.tenant == tenant,
            objects.c.owner == owner,
            objects.c.session_id == session_id,
        )
        .order_by(objects.c.id),
        "events": select(events)
        .where(
            events.c.tenant == tenant,
            events.c.owner == owner,
            events.c.session_id == session_id,
        )
        .order_by(events.c.seq, events.c.id),
        "jobs": select(jobs)
        .where(
            jobs.c.tenant_id == tenant,
            jobs.c.owner_id == owner,
            jobs.c.project_id == project_id,
        )
        .order_by(jobs.c.id),
        "assets": select(asset_table)
        .where(
            asset_table.c.tenant == tenant,
            asset_table.c.owner == owner,
            asset_table.c.project_id == project_id,
        )
        .order_by(asset_table.c.id),
        "obligations": select(objects)
        .where(
            objects.c.tenant == tenant,
            objects.c.owner == owner,
            objects.c.kind.in_(("history_action", "universal_purge")),
        )
        .order_by(objects.c.id),
        "references": select(documents)
        .where(
            documents.c.tenant_id == tenant,
            documents.c.owner_id == owner,
            or_(
                documents.c.project_id == project_id,
                documents.c.project_id == "__trash",
                documents.c.project_id == "__library",
            ),
        )
        .order_by(documents.c.project_id, documents.c.kind, documents.c.document_id),
    }
    for name, query in queries.items():
        rows = [
            dict(row)
            for row in connection.execute(query.limit(max_rows + 1)).mappings()
        ]
        require(len(rows) <= max_rows, "inventory_limit_exceeded")
        closure[name] = rows
    ids = [row["id"] for row in closure["jobs"]]
    for name, table in (("artifacts", artifacts), ("attempts", attempts)):
        rows = (
            [
                dict(row)
                for row in connection.execute(
                    select(table)
                    .where(table.c.job_id.in_(ids))
                    .order_by(table.c.id)
                    .limit(max_rows + 1)
                ).mappings()
            ]
            if ids
            else []
        )
        require(len(rows) <= max_rows, "inventory_limit_exceeded")
        closure[name] = rows
    from .artifact_writer import write_receipts
    from .storage_asset_journal import receipts

    optional = {
        "uploads": select(receipts)
        .where(
            receipts.c.tenant == tenant,
            receipts.c.owner == owner,
            receipts.c.project_id == project_id,
        )
        .order_by(receipts.c.id),
        "collections": select(write_receipts)
        .where(
            write_receipts.c.tenant == tenant,
            write_receipts.c.owner == owner,
            write_receipts.c.job_id.in_(ids),
        )
        .order_by(write_receipts.c.id),
        "workers": select(registered_workers)
        .where(registered_workers.c.current_job_id.in_(ids))
        .order_by(registered_workers.c.id),
    }
    tables = set(inspect(connection).get_table_names())
    for name, query in optional.items():
        require(
            all(table.name in tables for table in query.get_final_froms()),
            "migration_schema_unavailable",
        )
        rows = [
            dict(row)
            for row in connection.execute(query.limit(max_rows + 1)).mappings()
        ]
        require(len(rows) <= max_rows, "inventory_limit_exceeded")
        closure[name] = rows
    return closure


def _analyze(closure, *, max_project_bytes):
    session, row = closure["session"], closure["project"]
    source, project = session["payload"], row["payload"]
    require(
        isinstance(project, dict) and project.get("id") == row["document_id"],
        "project_binding_invalid",
    )
    require(project.get("schemaVersion") == 4, "project_schema_unsupported")
    require(
        project.get("integration_kind") == "quick_chat"
        and project.get("journey", {}).get("integration_kind") == "quick_chat"
        and project["journey"].get("quickChatSessionId") == session["id"],
        "managed_binding_invalid",
    )
    require("storyChat" not in project.get("journey", {}), "story_chat_unsupported")
    require(
        source.get("history", {}).get("trash_state", "active") == "active",
        "session_not_active",
    )
    require(not source.get("active_turn_id"), "assistant_active_or_uncertain")
    require(
        not source.get("history", {}).get("archive_pending"),
        "history_action_unresolved",
    )
    require(
        (source.get("title_generation") or {}).get("status")
        not in {"pending", "running", "unknown"},
        "authoring_operation_unresolved",
    )
    require(
        all(
            job["status"] in TERMINAL
            and job["lease_worker_id"] is None
            and job["lease_expires_at"] is None
            for job in closure["jobs"]
        ),
        "jobs_active_or_uncertain",
    )
    require(
        all(
            not (
                a["submission_started_at"] is not None
                or a["upstream_task_id"] is not None
            )
            or a["upstream_stopped"] == 1
            for a in closure["attempts"]
        ),
        "upstream_stop_unconfirmed",
    )
    require(
        all(w["state"] == "retired" for w in closure["workers"]),
        "worker_obligation_unresolved",
    )
    for obligation in closure["obligations"]:
        value = obligation["payload"]
        if value.get("state") != "completed" and session["id"] in value.get(
            "plan", {}
        ).get("session_ids", []):
            raise MigrationError("history_action_unresolved")
    for reference in closure["references"]:
        value = reference["payload"]
        if (
            reference["project_id"] == "__trash"
            and value.get("state") in {"trashed", "purging", "purged"}
            and value.get("target", {}).get("project_id") == row["document_id"]
        ):
            raise MigrationError("project_content_in_trash")
        if (
            reference["kind"] == "library_import"
            and value.get("state") not in {"completed", "blocked"}
            and row["document_id"]
            in {
                value.get("request", {}).get("source", {}).get("project_id"),
                value.get("request", {}).get("target_project_id"),
            }
        ):
            raise MigrationError("authoring_operation_unresolved")
    for upload in closure["uploads"]:
        value = json.loads(upload["record"])
        require(
            not value.get("busy")
            and all(
                spec.get("phase") == "verified"
                for spec in value.get("objects", {}).values()
            ),
            "upload_unresolved",
        )
    for collection in closure["collections"]:
        value = json.loads(collection["record"])
        require(
            value.get("phase") == "settled"
            and value.get("display", {}).get("phase") in {None, "complete", "missing"},
            "collection_unresolved",
        )
    children = closure["objects"]
    require(
        all(isinstance(r["payload"], dict) for r in children), "source_payload_invalid"
    )
    for child in children:
        require(
            child["kind"]
            in {
                "session",
                "turn",
                "card",
                "revision",
                "item",
                "execution",
                "preflight",
                "submission",
                "import",
                "result_import",
                "take_annotation",
                "take_annotation_receipt",
            },
            "source_kind_unsupported",
        )
        value = child["payload"]
        require(
            not (
                child["kind"] in {"turn", "import", "result_import"}
                and value.get("status") in {"pending", "running", "unknown"}
            ),
            "authoring_operation_unresolved",
        )
        require(
            not (
                child["kind"] == "execution"
                and (
                    value.get("status")
                    in {"pending_admission", "unknown", "recovery_hold"}
                    or value.get("status") == "admission_blocked"
                    and not value.get("job_id")
                )
            ),
            "admission_unresolved",
        )
    entities = project.get("entities")
    require(
        isinstance(entities, list)
        and all(
            isinstance(e, dict) and isinstance(e.get("data"), dict) for e in entities
        ),
        "project_entities_invalid",
    )
    by_id = {entity["id"]: entity for entity in entities}
    require(len(by_id) == len(entities), "project_entity_id_conflict")
    cards = {r["id"]: r for r in children if r["kind"] == "card"}
    revisions = [r for r in children if r["kind"] == "revision"]
    assets_by_id = {a["id"]: a for a in closure["assets"]}
    jobs_by_id = {j["id"]: j for j in closure["jobs"]}
    artifacts_by_id = {a["id"]: a for a in closure["artifacts"]}
    item_map = {}
    referenced_assets = set()
    for revision in revisions:
        p = revision["payload"]
        require(
            revision["parent_id"] in cards
            and p.get("card_id") == revision["parent_id"],
            "revision_card_binding_invalid",
        )
        require(
            isinstance(p.get("items"), list) and p["items"], "revision_items_invalid"
        )
        for _, _, ident in input_entries(p.get("inputs", {})):
            require(
                ident in assets_by_id and assets_by_id[ident]["status"] == "ready",
                "reference_binding_missing",
            )
            referenced_assets.add(ident)
        for item in p["items"]:
            require(
                isinstance(item, dict)
                and isinstance(item.get("id"), str)
                and item["id"] not in item_map,
                "take_identity_conflict",
            )
            shot = by_id.get(item.get("shot_id"))
            require(
                shot is not None and shot.get("type") == "shot",
                "projected_shot_missing",
            )
            pin = shot["data"].get("quickChatProjection", {})
            require(
                pin.get("session_id") == session["id"]
                and pin.get("revision_id") == revision["id"]
                and pin.get("item_id") == item["id"]
                and pin.get("requested_input_hash")
                == p.get("requested_input_hash", p.get("input_hash")),
                "projection_binding_changed",
            )
            proof = _projection_matches(project, shot, item)
            require(proof, "projection_content_changed")
            item_map[item["id"]] = {
                "revision_id": revision["id"],
                "card_id": revision["parent_id"],
                "shot_id": shot["id"],
                "projection_proof": proof,
                "projection_hash": item["projection_hash"],
            }
    for card in cards.values():
        require(
            any(
                r["id"] == card["payload"].get("current_revision_id")
                and r["parent_id"] == card["id"]
                for r in revisions
            ),
            "current_revision_missing",
        )
    for child in children:
        if child["kind"] == "item":
            p = child["payload"]
            require(
                child["id"] in item_map
                and p.get("revision_id") == item_map[child["id"]]["revision_id"]
                and p.get("shot_id") == item_map[child["id"]]["shot_id"],
                "item_projection_binding_unknown",
            )
        if child["kind"] == "execution":
            require(
                child["payload"].get("item_id") in item_map
                and child["parent_id"] == child["payload"]["item_id"],
                "execution_item_binding_unknown",
            )
        if child["kind"] == "execution" and child["payload"].get("job_id"):
            job = jobs_by_id.get(child["payload"]["job_id"])
            require(
                job is not None and child["payload"].get("item_id") in item_map,
                "execution_job_binding_unknown",
            )
            require(
                job["request"].get("client_ref", {}).get("shot_id")
                == item_map[child["payload"]["item_id"]]["shot_id"],
                "job_shot_binding_changed",
            )
    selected = 0
    for shot in (e for e in entities if e.get("type") == "shot"):
        ident = shot["data"].get("selectedAssetId")
        selected += bool(ident)
        candidates = shot["data"].get("candidateIds", [])
        require(
            isinstance(candidates, list)
            and all(isinstance(value, str) for value in candidates),
            "candidate_media_binding_unknown",
        )
        for media_id in set(candidates + ([ident] if ident else [])):
            media = by_id.get(media_id)
            require(
                media is not None and media["type"] in {"video", "image"},
                "selected_media_binding_unknown",
            )
            artifact_id = media["data"].get("cloudArtifactId")
            if artifact_id:
                artifact = artifacts_by_id.get(artifact_id)
                require(
                    artifact is not None
                    and artifact["job_id"] in jobs_by_id
                    and jobs_by_id[artifact["job_id"]]["status"] == "succeeded"
                    and not artifact["metadata"].get("content_deleted")
                    and artifact["metadata"].get("kind") == media["type"]
                    and jobs_by_id[artifact["job_id"]]["request"]
                    .get("client_ref", {})
                    .get("shot_id")
                    == shot["id"],
                    "selected_artifact_binding_unknown",
                )
            else:
                require(
                    media["data"].get("cloudAssetId") in assets_by_id
                    and assets_by_id[media["data"]["cloudAssetId"]]["status"]
                    == "ready",
                    "selected_media_binding_unknown",
                )
    turns = sorted(
        (r for r in children if r["kind"] == "turn"),
        key=lambda r: (r["payload"].get("seq", float("inf")), r["created_at"], r["id"]),
    )
    messages = []
    for turn in turns:
        for role, field in (("user", "text"), ("assistant", "reply")):
            text = turn["payload"].get(field)
            if isinstance(text, str) and text.strip():
                messages.append(
                    {
                        "id": turn["id"] + ("-u" if role == "user" else "-a"),
                        "role": role,
                        "text": text,
                        "imported": True,
                        "source_session_id": session["id"],
                        "source_turn_id": turn["id"],
                    }
                )
    result, canonical_shots, scene_id, grouping_counts, archive = _group_shots(
        project, session, cards, revisions, item_map, closure
    )
    result.pop("integration_kind")
    result["journey"].pop("integration_kind")
    result["journey"]["storyChat"] = {
        "version": 1,
        "language": "zh",
        "sceneId": scene_id,
        "turns": len(turns),
        "messages": messages,
    }
    result["journey"]["quickChatMigration"] = {
        "session_id": session["id"],
        "contract": CONTRACT,
        "mapping_version": MAPPING_VERSION,
        "receipt_id": session["id"],
    }
    validate_project(result, max_project_bytes)
    analysis = {
        "project": result,
        "mapping": item_map,
        "canonical_shots": canonical_shots,
        **archive,
        "mapping_refusals": [],
        "counts": {
            "cards": len(cards),
            "revisions": len(revisions),
            "takes": len(item_map),
            "projected_shots": len(item_map),
            "persisted_selected": selected,
            "messages": len(messages),
            "user_messages": sum(m["role"] == "user" for m in messages),
            "assistant_messages": sum(m["role"] == "assistant" for m in messages),
            "referenced_assets_metadata_valid": len(referenced_assets),
            "outcome_only_normalized_projections": sum(
                item["projection_proof"] == "outcome_only_normalized_hash"
                for item in item_map.values()
            ),
            **grouping_counts,
        },
    }
    analysis["archive_payload_bytes"] = _bounded_payload(
        _archive_payload(project, analysis), max_project_bytes
    )
    return analysis


def _census(closure):
    """Raw owner inventory also survives a mapping refusal; unknown is not zero."""
    children = closure["objects"]
    revisions = [r for r in children if r["kind"] == "revision"]
    turns = [r for r in children if r["kind"] == "turn"]
    project = closure["project"]["payload"]
    entities = project.get("entities") if isinstance(project, dict) else None
    return {
        "cards": sum(r["kind"] == "card" for r in children),
        "revisions": len(revisions),
        "takes": sum(len(r["payload"]["items"]) for r in revisions)
        if all(
            isinstance(r["payload"], dict)
            and isinstance(r["payload"].get("items"), list)
            for r in revisions
        )
        else None,
        "persisted_selected": sum(
            bool(e["data"].get("selectedAssetId"))
            for e in entities
            if e.get("type") == "shot"
        )
        if isinstance(entities, list)
        and all(
            isinstance(e, dict) and isinstance(e.get("data"), dict) for e in entities
        )
        else None,
        "turns": len(turns),
        "text_turns": sum(
            isinstance(r["payload"].get("text"), str)
            and bool(r["payload"]["text"].strip())
            for r in turns
        )
        if all(isinstance(r["payload"], dict) for r in turns)
        else None,
    }


def preview(connection, tenant, owner, session_id, *, max_project_bytes=8 * 1024**2):
    closure = _load(connection, tenant, owner, session_id)
    raw_counts = _census(closure)
    original = (
        connection.execute(
            select(documents).where(
                *_receipt_where(
                    tenant, owner, closure["project"]["document_id"], session_id
                )
            )
        )
        .mappings()
        .first()
    )
    if original and _completed_rollback(original):
        return {
            "session_id": session_id,
            "owner": owner,
            "project_id": closure["project"]["document_id"],
            "state": "rolled_back",
            "raw_counts": raw_counts,
            "refusals": ["new_conversion_after_rollback_requires_review"],
        }
    migrated = migrated_project_id(connection, tenant, owner, closure["session"])
    if migrated:
        return {
            "session_id": session_id,
            "owner": owner,
            "project_id": migrated,
            "state": "already_migrated",
            "raw_counts": raw_counts,
        }
    try:
        analysis = _analyze(closure, max_project_bytes=max_project_bytes)
    except MigrationError as error:
        return {
            "session_id": session_id,
            "owner": owner,
            "project_id": closure["project"]["document_id"],
            "session_version": closure["session"]["version"],
            "project_version": closure["project"]["version"],
            "source_hash": request_hash(closure),
            "project_hash": request_hash(closure["project"]["payload"]),
            "state": "refused",
            "raw_counts": raw_counts,
            "refusals": [error.code],
            "media_validation": "metadata_only",
        }
    return {
        "session_id": session_id,
        "owner": owner,
        "project_id": closure["project"]["document_id"],
        "session_version": closure["session"]["version"],
        "project_version": closure["project"]["version"],
        "source_hash": request_hash(closure),
        "project_hash": request_hash(closure["project"]["payload"]),
        "target_hash": request_hash(analysis["project"]),
        "counts": analysis["counts"],
        "raw_counts": raw_counts,
        "mapping": analysis["mapping"],
        "canonical_shots": analysis["canonical_shots"],
        "archive_strategy": analysis["archive_strategy"],
        "archive_payload_bytes": analysis["archive_payload_bytes"],
        "state": "refused" if analysis["mapping_refusals"] else "ready",
        "refusals": analysis["mapping_refusals"],
        "media_validation": "metadata_only",
        "projection_validation": "original_hash_or_proven_outcome_only_delta; no media bytes read",
    }


def apply_staging(
    connection,
    settings,
    owner,
    session_id,
    *,
    source_hash,
    project_version,
    actor_user_id,
    reader_evidence=None,
    now=None,
):
    require(
        settings.deployment_target == "staging"
        and settings.public_origin == "https://staging.inkseq.com",
        "production_apply_not_authorized",
    )
    require(connection.in_transaction(), "migration_transaction_required")
    actor = (
        connection.execute(
            select(users).where(
                users.c.tenant == settings.tenant_id,
                users.c.id == actor_user_id,
                users.c.role == "admin",
                users.c.status == "active",
            )
        )
        .mappings()
        .first()
    )
    require(actor is not None, "active_admin_actor_required")
    owner_reference_gate(connection, settings.tenant_id, owner)
    closure = _load(connection, settings.tenant_id, owner, session_id, lock=True)
    session, row = closure["session"], closure["project"]
    previous = (
        connection.execute(
            select(documents).where(
                *_receipt_where(
                    settings.tenant_id, owner, row["document_id"], session_id
                )
            )
        )
        .mappings()
        .first()
    )
    if previous:
        require(
            previous["payload"].get("source_hash") == source_hash
            and migrated_project_id(connection, settings.tenant_id, owner, session)
            == row["document_id"],
            "migration_receipt_conflict",
        )
        return {**previous["payload"], "replayed": True}
    require(
        row["version"] == project_version and request_hash(closure) == source_hash,
        "migration_source_changed",
    )
    assert_project_writable(connection, settings.tenant_id, owner, row["document_id"])
    readers = validate_reader_evidence(reader_evidence)
    analysis = _analyze(closure, max_project_bytes=settings.max_project_bytes)
    require(
        not analysis["mapping_refusals"],
        analysis["mapping_refusals"][0]
        if analysis["mapping_refusals"]
        else "mapping_invalid",
    )
    target = analysis["project"]
    assert_reference_payload(connection, settings.tenant_id, owner, target)
    at = time.time() if now is None else now
    receipt = {
        "contract": CONTRACT,
        "mapping_version": MAPPING_VERSION,
        "receipt_id": session_id,
        "tenant_id": settings.tenant_id,
        "owner_id": owner,
        "status": "completed",
        "source_session_id": session_id,
        "source_hash": source_hash,
        "source_rows_hash": _source_rows_hash(closure),
        "operational_closure_hash": _operational_hash(closure),
        "source_session_version": session["version"],
        "project_id": row["document_id"],
        "before_project_version": project_version,
        "after_project_version": project_version + 1,
        "before_project_hash": request_hash(row["payload"]),
        "after_project_hash": request_hash(target),
        **_archive_payload(row["payload"], analysis),
        "counts": analysis["counts"],
        "reader_self_check": readers,
        "reader_self_check_hash": request_hash(readers),
        "actor_user_id": actor["id"],
        "actor_owner": actor["owner"],
        "completed_at": at,
    }
    _bounded_payload(receipt, settings.max_project_bytes)
    # Reserve enough of the SAME existing bound for an audited rollback. Never
    # create an archive that fits but cannot later record its restoration.
    _bounded_payload(
        {
            **receipt,
            "rollback": _rollback_record(
                receipt,
                {"id": "🔁" * 36, "owner": "🔁" * 80},
                9999999999.999999,
                "🔁" * 500,
            ),
        },
        settings.max_project_bytes,
    )
    changed = connection.execute(
        update(documents)
        .where(
            *_project_where(settings.tenant_id, owner, row["document_id"]),
            documents.c.version == project_version,
        )
        .values(payload=canonical(target), version=project_version + 1, updated_at=at)
    )
    require(changed.rowcount == 1, "migration_project_changed")
    connection.execute(
        insert(documents).values(
            tenant_id=settings.tenant_id,
            owner_id=owner,
            project_id=row["document_id"],
            kind=KIND,
            document_id=session_id,
            version=1,
            payload=receipt,
            updated_at=at,
        )
    )
    return {**receipt, "replayed": False}


def rollback_staging(
    connection,
    settings,
    owner,
    session_id,
    *,
    source_hash,
    actor_user_id,
    reason,
    now=None,
):
    require(
        settings.deployment_target == "staging"
        and settings.public_origin == "https://staging.inkseq.com",
        "production_rollback_not_authorized",
    )
    require(connection.in_transaction(), "migration_transaction_required")
    require(
        isinstance(reason, str) and bool(reason.strip()) and len(reason) <= 500,
        "rollback_reason_required",
    )
    actor = (
        connection.execute(
            select(users).where(
                users.c.tenant == settings.tenant_id,
                users.c.id == actor_user_id,
                users.c.role == "admin",
                users.c.status == "active",
            )
        )
        .mappings()
        .first()
    )
    require(actor is not None, "active_admin_actor_required")
    owner_reference_gate(connection, settings.tenant_id, owner)
    closure = _load(connection, settings.tenant_id, owner, session_id, lock=True)
    project = closure["project"]
    original = (
        connection.execute(
            select(documents)
            .where(
                *_receipt_where(
                    settings.tenant_id, owner, project["document_id"], session_id
                )
            )
            .with_for_update()
        )
        .mappings()
        .first()
    )
    require(
        original is not None and isinstance(original["payload"], dict),
        "migration_receipt_missing",
    )
    receipt = original["payload"]
    require(receipt.get("source_hash") == source_hash, "migration_receipt_conflict")
    if "rollback" in receipt:
        require(_completed_rollback(original), "rollback_outcome_unknown")
        return {
            **receipt,
            "replayed": True,
            "current_project_hash": request_hash(project["payload"]),
            "current_project_version": project["version"],
        }
    require(
        migrated_project_id(connection, settings.tenant_id, owner, closure["session"])
        == project["document_id"],
        "migration_receipt_conflict",
    )
    require(
        project["version"] == receipt["after_project_version"]
        and request_hash(project["payload"]) == receipt["after_project_hash"],
        "rollback_project_changed",
    )
    require(
        receipt.get("source_rows_hash") == _source_rows_hash(closure),
        "rollback_source_changed",
    )
    require(
        receipt.get("operational_closure_hash") == _operational_hash(closure),
        "rollback_work_or_media_changed",
    )
    require(
        all(
            j["status"] in TERMINAL
            and j["lease_worker_id"] is None
            and j["lease_expires_at"] is None
            for j in closure["jobs"]
        )
        and all(
            (a["submission_started_at"] is None and a["upstream_task_id"] is None)
            or a["upstream_stopped"] == 1
            for a in closure["attempts"]
        )
        and all(w["state"] == "retired" for w in closure["workers"]),
        "rollback_work_unresolved",
    )
    before = receipt.get("before_project")
    require(
        isinstance(before, dict)
        and before.get("id") == project["document_id"]
        and request_hash(before) == receipt["before_project_hash"],
        "rollback_archive_invalid",
    )
    validate_project(before, settings.max_project_bytes)
    assert_project_writable(
        connection, settings.tenant_id, owner, project["document_id"]
    )
    assert_reference_payload(connection, settings.tenant_id, owner, before)
    at = time.time() if now is None else now
    outcome = {**receipt, "rollback": _rollback_record(receipt, actor, at, reason)}
    _bounded_payload(outcome, settings.max_project_bytes)
    changed = connection.execute(
        update(documents)
        .where(
            *_project_where(settings.tenant_id, owner, project["document_id"]),
            documents.c.version == receipt["after_project_version"],
        )
        .values(
            payload=canonical(before),
            version=receipt["after_project_version"] + 1,
            updated_at=at,
        )
    )
    require(changed.rowcount == 1, "rollback_project_changed")
    changed = connection.execute(
        update(documents)
        .where(
            *_receipt_where(
                settings.tenant_id, owner, project["document_id"], session_id
            ),
            documents.c.version == 1,
        )
        .values(payload=canonical(outcome), version=2, updated_at=at)
    )
    require(changed.rowcount == 1, "rollback_receipt_changed")
    return {**outcome, "replayed": False}
