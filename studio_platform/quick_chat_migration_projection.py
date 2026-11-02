"""Pure legacy story projection/grouping; does not read or write database rows."""

from __future__ import annotations

import copy
import math
import uuid

from .source_snapshot import source_snapshot


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
                for a, b in zip(by_version, by_version[1:], strict=False)
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


# Existing callers and archived exception references keep the same public path.
MigrationError.__module__ = "studio_platform.quick_chat_migration"
