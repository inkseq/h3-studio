"""Content-free audit trail for committed browser and agent story edits.

This is an additive table, created under the platform startup DDL lock. Old
documents are deliberately not backfilled: their editing actor is unknown.
Append uses the document's transaction; failed edits and idempotent replays
cannot leave a second or misleading activity entry. Job progress has its own
read API and is never converted into write-on-poll events here.
"""

from __future__ import annotations

from datetime import datetime, timezone
import re
import uuid

from fastapi import Query, Request
from sqlalchemy import (
    Column,
    Float,
    Integer,
    JSON,
    MetaData,
    String,
    Table,
    UniqueConstraint,
    insert,
    select,
)

from .auth import personal_keys


metadata = MetaData()
activity = Table(
    "platform_project_activity",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("tenant_id", String(200), nullable=False),
    Column("owner_id", String(200), nullable=False),
    Column("project_id", String(200), nullable=False),
    Column("project_version", Integer, nullable=False),
    Column("actor_kind", String(20), nullable=False),
    Column("actor_label", String(80), nullable=False),
    Column("event_type", String(40), nullable=False),
    Column("summary", String(300), nullable=False),
    Column("operations", JSON, nullable=False),
    Column("target_entity_ids", JSON, nullable=False),
    Column("target_count", Integer, nullable=False),
    Column("occurred_at", Float, nullable=False),
    UniqueConstraint("tenant_id", "owner_id", "project_id", "project_version"),
)

OP_LABELS = {
    "entity.create": "新增节点",
    "entity.update": "修改节点",
    "entity.delete": "删除节点",
    "link.create": "关联参考",
    "link.delete": "移除关联",
    "project.update": "修改故事信息",
    "journey.update": "调整制作流程",
    "layout.update": "调整画布",
    "asset.attach": "加入素材",
    "artifact.adopt": "加入生成结果",
    "shot.select": "选择镜头版本",
    "shot.trim": "调整镜头片段",
    "sound.generated": "关联生成声音",
    "captions.set": "编辑字幕",
    "captions.confirm": "确认字幕",
    "sound.set": "调整声音轨道",
}


def _targets(before, after, actions):
    """Only entity IDs survive; no patch values, prompts, titles, or file URLs."""
    before, after = before or {}, after or {}
    old = {e["id"]: e for e in before.get("entities", [])}
    new = {e["id"]: e for e in after.get("entities", [])}
    changed = {
        ident for ident in old.keys() | new.keys() if old.get(ident) != new.get(ident)
    }
    for section in ("soundTracks", "captionTracks"):
        first = before.get("journey", {}).get(section, {})
        second = after.get("journey", {}).get(section, {})
        changed.update(
            key
            for key in first.keys() | second.keys()
            if first.get(key) != second.get(key)
        )
    links_before = {link["id"]: link for link in before.get("links", [])}
    links_after = {link["id"]: link for link in after.get("links", [])}
    for ident in links_before.keys() | links_after.keys():
        if links_before.get(ident) != links_after.get(ident):
            for link in (links_before.get(ident), links_after.get(ident)):
                if link:
                    changed.update((link["source"], link["target"]))
    # Explicit action targets appear first so an adopted result opens its shot.
    preferred = []
    for action in actions:
        preferred.extend(
            action.get(key)
            for key in ("shot_id", "chapter_id", "entity_id", "parent_id")
        )
    known = old.keys() | new.keys()
    ordered = list(
        dict.fromkeys(
            ident for ident in [*preferred, *sorted(changed)] if ident in known
        )
    )
    return ordered[:100], len(ordered)


def append_activity(
    connection,
    *,
    tenant_id,
    principal,
    project_id,
    version,
    occurred_at,
    before,
    after,
    actions=(),
    event_type="project.edited",
):
    if event_type not in {"project.created", "project.saved", "project.edited"}:
        raise ValueError("invalid_project_activity_type")
    from .agent_management import record_created, record_event

    record_event(connection, tenant_id, principal, event_type, occurred_at)
    if before is None:
        record_created(
            connection,
            tenant_id,
            principal,
            'project',
            project_id,
            occurred_at,
            project_id,
        )
    from .project_validation import TYPES

    old_ids = {e['id'] for e in (before or {}).get('entities', [])}
    for entity in (after or {}).get('entities', []):
        kind = entity.get('type')
        # Legacy/minimal documents retain their audit history without invented attribution.
        if entity['id'] not in old_ids and isinstance(kind, str) and kind in TYPES:
            record_created(
                connection,
                tenant_id,
                principal,
                kind,
                entity['id'],
                occurred_at,
                project_id,
            )
    # Read only the explicit display-name field, never tokens, prefixes, hashes,
    # credential IDs, document bodies, or service client identifiers.
    actor_label = principal.owner
    if principal.machine and principal.actor_id.startswith("key:"):
        label = connection.execute(
            select(personal_keys.c.name).where(
                personal_keys.c.tenant == tenant_id,
                personal_keys.c.owner == principal.owner,
                personal_keys.c.id == principal.actor_id[4:],
            )
        ).scalar_one_or_none()
        if label:
            actor_label = label
    actor_label = re.sub(r"[\x00-\x1f\x7f]", " ", actor_label)[:80]
    operations = list(dict.fromkeys(a["op"] for a in actions if a["op"] in OP_LABELS))
    if event_type == "project.created":
        summary = "创建故事"
    elif event_type == "project.saved":
        summary = "保存故事修改"
    elif len(actions) == 1:
        summary = OP_LABELS[operations[0]] if operations else "编辑故事"
    else:
        labels = "、".join(OP_LABELS[op] for op in operations[:4])
        summary = f"完成 {len(actions)} 项编辑：{labels}" + (
            "等" if len(operations) > 4 else ""
        )
    targets, count = _targets(before, after, actions)
    connection.execute(
        insert(activity).values(
            id=str(uuid.uuid4()),
            tenant_id=tenant_id,
            owner_id=principal.owner,
            project_id=project_id,
            project_version=version,
            actor_kind="api_key" if principal.machine else "browser",
            actor_label=actor_label,
            event_type=event_type,
            summary=summary,
            operations=operations,
            target_entity_ids=targets,
            target_count=count,
            occurred_at=occurred_at,
        )
    )


def register_routes(app):
    @app.get("/v1/projects/{project_id}/activity")
    def project_activity(
        project_id: str,
        request: Request,
        limit: int = Query(50, ge=1, le=100),
        before_version: int | None = Query(None, ge=1, le=2147483647),
    ):
        principal = request.state.principal
        app.state.authorized_project(principal, project_id, "projects:read")
        statement = select(activity).where(
            activity.c.tenant_id == app.state.settings.tenant_id,
            activity.c.owner_id == principal.owner,
            activity.c.project_id == project_id,
        )
        if before_version is not None:
            statement = statement.where(activity.c.project_version < before_version)
        with app.state.repository.engine.connect() as connection:
            rows = list(
                connection.execute(
                    statement.order_by(activity.c.project_version.desc()).limit(
                        limit + 1
                    )
                ).mappings()
            )
        items = [
            {
                key: row[key]
                for key in (
                    "id",
                    "project_version",
                    "actor_kind",
                    "actor_label",
                    "event_type",
                    "summary",
                    "operations",
                    "target_entity_ids",
                    "target_count",
                )
            }
            for row in rows[:limit]
        ]
        for item, row in zip(items, rows):
            item["occurred_at"] = (
                datetime.fromtimestamp(row["occurred_at"], timezone.utc)
                .isoformat()
                .replace("+00:00", "Z")
            )
        return {
            "project_id": project_id,
            "items": items,
            "next_before_version": items[-1]["project_version"]
            if len(rows) > limit
            else None,
        }
