"""Atomic, versioned editing operations over the same documents the browser uses.

No generation or external URL fetching happens here. Asset references are resolved
by owner/project before entering the transaction, then committed with the document.
"""

from __future__ import annotations

import copy
from datetime import datetime, timezone
import math
import re
import uuid

from fastapi import HTTPException
from sqlalchemy import (
    Column,
    Float,
    JSON,
    MetaData,
    String,
    Table,
    insert,
    select,
    update,
)
from sqlalchemy.exc import IntegrityError

from .render_limits import MAX_AUDIO_TRACKS
from .caption_server import caption_signature
from .guided_schema import (
    validate_action_fields,
)
from .project_validation import ID, TYPES, validate_project
from .project_activity import (
    append_activity,
    metadata as activity_metadata,
    register_routes as register_activity_routes,
)
from .render_plans import ordered_chapter
from .repository import (
    Conflict,
    NotFound,
    Scope,
    canonical,
    documents,
    request_hash,
    artifacts,
    jobs,
)
from .content_lifecycle import (
    owner_reference_gate,
    assert_project_writable,
    assert_reference_payload,
)
from .shot_segments import (
    has_segments,
    segment_frames,
    source_binding,
    validate_segments_data,
    validate_segments_transition,
)
from .inky_hooks import queue_refresh

metadata = MetaData()
receipts = Table(
    "platform_edit_receipts",
    metadata,
    Column("tenant", String(200), primary_key=True),
    Column("owner", String(200), primary_key=True),
    Column("actor", String(200), primary_key=True),
    Column("project_id", String(200), primary_key=True),
    Column("key", String(160), primary_key=True),
    Column("fingerprint", String(64), nullable=False),
    Column("response", JSON, nullable=False),
    Column("created_at", Float, nullable=False),
)


def now_iso():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def empty_project(body):
    if set(body) - {"id", "title", "logline", "workspace"}:
        raise ValueError(
            "新故事仅接受id、title、logline、workspace；导入完整文稿请使用project字段"
        )
    if "workspace" in body and body["workspace"] != "freestyle":
        raise ValueError("workspace目前仅支持freestyle；普通故事省略该字段")
    project = dict(
        schemaVersion=4,
        id=body.get("id", "project-" + uuid.uuid4().hex),
        title=body.get("title"),
        logline=body.get("logline", ""),
        entities=[],
        links=[],
        jobs=[],
        journey={"stage": 1},
        layout={"positions": {}, "viewport": {"x": 0, "y": 0, "zoom": 1}},
        updatedAt=now_iso(),
    )
    if body.get("workspace") == "freestyle":
        parent = None
        for kind, title in (
            ("chapter", "快速创作"),
            ("scene", "单镜场景"),
            ("shot", "我的视频"),
        ):
            ident = (
                kind
                + "-"
                + uuid.uuid5(uuid.NAMESPACE_URL, str(project["id"]) + ":" + kind).hex
            )
            new_entity(
                project,
                {
                    "id": ident,
                    "type": kind,
                    "title": title,
                    "parentId": parent,
                    "data": {"seconds": 5, "prompt": ""}
                    if kind == "shot"
                    else {"script": ""}
                    if kind == "scene"
                    else {},
                },
            )
            parent = ident
        project["journey"] = {
            "workspace": "freestyle",
            "stage": 4,
            "reviewShotId": parent,
        }
    return project


def object_fields(value, allowed, message):
    if not isinstance(value, dict) or set(value) - set(allowed):
        raise ValueError(message)
    return value


def entity_by_id(project, entity_id, kinds=None):
    entity = next((e for e in project["entities"] if e["id"] == entity_id), None)
    if entity is None or kinds and entity["type"] not in kinds:
        raise NotFound("entity_not_found")
    return entity


def new_entity(project, value):
    object_fields(
        value,
        {"id", "type", "parentId", "title", "description", "data", "status", "order"},
        "节点创建字段无效",
    )
    kind = value.get("type")
    if not isinstance(kind, str) or kind not in TYPES:
        raise ValueError("节点类型无效")
    parent = value.get("parentId")
    data = (
        {"seconds": 5, "prompt": ""}
        if kind == "shot"
        else {"recipe": "video"}
        if kind == "generation"
        else {}
    )
    entity = dict(
        id=value.get("id", kind + "-" + uuid.uuid4().hex),
        type=kind,
        parentId=parent,
        title=value.get("title", ""),
        description=value.get("description", ""),
        version=1,
        order=value.get(
            "order", len([e for e in project["entities"] if e["parentId"] == parent])
        ),
        status=value.get("status", "draft"),
        data=value.get("data", data),
    )
    project["entities"].append(entity)
    return entity


def update_entity(entity, patch):
    object_fields(
        patch,
        {"title", "description", "parentId", "order", "status", "data"},
        "节点修改字段无效；id、type、version不能覆盖",
    )
    value = copy.deepcopy(patch)
    if "data" in value:
        if not isinstance(value["data"], dict):
            raise ValueError("节点data必须为对象")
        value["data"] = {**entity["data"], **value["data"]}
    entity.update(value)
    entity["version"] += 1


def checked_captions(project, chapter_id):
    """SRT/editor checks; stricter burn-in validation stays in render planning."""
    shots = ordered_chapter(project, chapter_id)[2]
    from .render_timing import enabled, shot_frames

    edited_timing = enabled(project, chapter_id, shots)
    duration = (
        sum(
            shot_frames(s)[1]
            if edited_timing
            else max(0, math.floor(s["data"].get("seconds", 0) * 24 + 0.5))
            for s in shots
        )
        / 24
    )
    cues = (
        project.get("journey", {})
        .get("captionTracks", {})
        .get(chapter_id, {})
        .get("cues", [])
    )
    if (
        not isinstance(cues, list)
        or not 1 <= len(cues) <= 500
        or not 0 < duration < 360000
    ):
        raise ValueError("字幕需要有效的章节时长和1至500条字幕")
    previous = 0
    for cue in sorted(cues, key=lambda c: (c["start"], c["end"])):
        text = cue.get("text")
        start, end = cue.get("start"), cue.get("end")
        if (
            not isinstance(text, str)
            or not text.strip()
            or len(text) > 2000
            or re.search(r"[<>\x00-\x08\x0b\x0c\x0e-\x1f]", text)
            or any(
                type(v) not in (int, float) or not math.isfinite(v)
                for v in (start, end)
            )
            or not 0 <= start < end <= duration + 0.000001
            or math.floor(start * 1000 + 0.5) < previous
            or math.floor(end * 1000 + 0.5) <= math.floor(start * 1000 + 0.5)
        ):
            raise ValueError("字幕文字、时长、排序或重叠无效，请核对原稿")
        previous = math.floor(end * 1000 + 0.5)
    return shots, sorted(cues, key=lambda c: (c["start"], c["end"]))


def delete_entity(project, entity_id, cascade):
    entity_by_id(project, entity_id)
    if type(cascade) is not bool:
        raise ValueError("cascade必须为布尔值")
    deleted = {entity_id}
    while True:
        descendants = {e["id"] for e in project["entities"] if e["parentId"] in deleted}
        if descendants <= deleted:
            break
        if not cascade:
            raise Conflict("entity_has_children_use_explicit_cascade")
        deleted |= descendants
    removed_links = {
        l["id"]
        for l in project["links"]
        if l["source"] in deleted or l["target"] in deleted
    }
    project["entities"] = [e for e in project["entities"] if e["id"] not in deleted]
    project["links"] = [l for l in project["links"] if l["id"] not in removed_links]
    for entity in project["entities"]:
        before, data = copy.deepcopy(entity["data"]), entity["data"]
        if isinstance(data.get("cast"), list):
            data["cast"] = [
                c
                for c in data["cast"]
                if (c if isinstance(c, str) else c["characterId"]) not in deleted
            ]
        if entity["type"] in {"scene", "shot"} and data.get("locationId") in deleted:
            data["locationId"] = ""
        if entity["type"] == "location" and isinstance(
            data.get("referenceAssetIds"), list
        ):
            data["referenceAssetIds"] = [
                ident for ident in data["referenceAssetIds"] if ident not in deleted
            ]
        for look in data.get("looks", []):
            for slot, value in look.get("gallery", {}).items():
                if value in deleted:
                    look["gallery"][slot] = ""
                    look["version"] += 1
            if "imageIds" in look:
                retained = [ident for ident in look["imageIds"] if ident not in deleted]
                if retained != look["imageIds"]:
                    look["version"] += 1
                look["imageIds"] = retained
        if "libraryFileIds" in data:
            data["libraryFileIds"] = [
                ident for ident in data["libraryFileIds"] if ident not in deleted
            ]
        if "imageIds" in data:
            data["imageIds"] = [
                ident for ident in data["imageIds"] if ident not in deleted
            ]
        for version in data.get("versions", []):
            retained = [
                ident for ident in version.get("imageIds", []) if ident not in deleted
            ]
            if retained != version.get("imageIds", []):
                version["version"] += 1
            version["imageIds"] = retained
        if "voices" in data:
            data["voices"] = [
                voice for voice in data["voices"] if voice["assetId"] not in deleted
            ]
            voice_ids = {voice["id"] for voice in data["voices"]}
            if data.get("mainVoiceId") and data["mainVoiceId"] not in voice_ids:
                data["mainVoiceId"] = ""
            for look in data.get("looks", []):
                if look.get("voiceId") and look["voiceId"] not in voice_ids:
                    look["voiceId"] = ""
        if (data.get("ambient") or {}).get("assetId") in deleted:
            data["ambient"] = None
        if "storyboard" in data:
            draft = data["storyboard"]
            draft["propIds"] = [
                ident for ident in draft.get("propIds", []) if ident not in deleted
            ]
            draft["extraRefs"] = [
                ref
                for ref in draft.get("extraRefs", [])
                if ref["entityId"] not in deleted
            ]
            for key in ("firstFrameId", "lastFrameId"):
                if draft.get(key) in deleted:
                    draft[key] = ""
            if (draft.get("place") or {}).get("locationId") in deleted:
                draft["place"] = None
            if draft.get("previousFrame") and any(
                ident in deleted for ident in draft["previousFrame"].values()
            ):
                draft["previousFrame"] = None
        if data.get("selectedAssetId") in deleted:
            data["selectedAssetId"] = ""
        if isinstance(data.get("candidateIds"), list):
            data["candidateIds"] = [v for v in data["candidateIds"] if v not in deleted]
        h3 = data.get("h3", {})
        if isinstance(h3.get("guides"), list):
            h3["guides"] = [g for g in h3["guides"] if g.get("media_id") not in deleted]
        for key in removed_links:
            data.get("referenceRanges", {}).pop(key, None)
        for key in deleted:
            h3.get("video_audio", {}).pop(key, None)
        if before != data:
            entity["version"] += 1
            entity["status"] = "review"
    for key in deleted:
        project["layout"]["positions"].pop(key, None)
        for field in ("soundTracks", "captionTracks"):
            project.get("journey", {}).get(field, {}).pop(key, None)
    # Existing rendered files/jobs and stale sound/range edits survive deletion,
    # matching browser recoverability. Render planning blocks broken bindings.


class Guided:
    def __init__(self, app):
        self.app, self.repo = app, app.state.repository
        self.settings = app.state.settings
        with self.repo.transaction() as conn:
            if self.repo.engine.dialect.name == "postgresql":
                conn.exec_driver_sql("SELECT pg_advisory_xact_lock(685939796868749721)")
            metadata.create_all(conn)
            activity_metadata.create_all(conn)

    def envelope(self, row):
        return {
            "id": row["document_id"],
            "version": row["version"],
            "updated_at": row["updated_at"],
            "project": row["payload"],
        }

    def save(self, principal, project_id, project, expected_version):
        """Browser full-document save and its content-free activity are atomic."""
        from .quality_authoring import project as resolve_quality_project

        if (
            not isinstance(project, dict)
            or project.get("id") != project_id
            or type(expected_version) is not int
        ):
            raise ValueError("需要匹配的项目ID与版本")
        document_scope = Scope(
            self.settings.tenant_id, principal.owner, "__projects", principal.actor_id
        )
        where = (
            self.repo._scope(documents, document_scope),
            documents.c.kind == "project",
            documents.c.document_id == project_id,
        )
        segment_sources = self.resolve_segment_sources(principal, project_id, project)
        with self.repo.transaction() as conn:
            owner_reference_gate(conn, self.settings.tenant_id, principal.owner)
            assert_project_writable(
                conn, self.settings.tenant_id, principal.owner, project_id
            )
            row = self.repo._locked(conn, select(documents).where(*where))
            if row is None:
                raise NotFound("document_not_found")
            if row["version"] != expected_version:
                raise Conflict("document_version_conflict")
            project = resolve_quality_project(
                self.settings, self.repo, principal, project, baseline=row["payload"]
            )
            validate_segments_transition(row["payload"], project)
            self.check_segment_sources(project, segment_sources)
            validate_project(project, self.settings.max_project_bytes)
            assert_reference_payload(
                conn, self.settings.tenant_id, principal.owner, project
            )
            values = dict(
                payload=canonical(project),
                version=row["version"] + 1,
                updated_at=self.repo.clock(),
            )
            conn.execute(update(documents).where(*where).values(**values))
            append_activity(
                conn,
                tenant_id=self.settings.tenant_id,
                principal=principal,
                project_id=project_id,
                version=values["version"],
                occurred_at=values["updated_at"],
                before=row["payload"],
                after=project,
                event_type="project.saved",
            )
            queue_refresh(principal, project_id)
            return self.envelope({"document_id": project_id, **values})

    def mutate(self, principal, project_id, body, key=None, *, create=False):
        if key is not None and (not isinstance(key, str) or not ID.fullmatch(key)):
            raise ValueError("Idempotency-Key须为1至160个字母、数字、横线或下划线")
        fingerprint = request_hash(
            {
                "create": create,
                "body": {k: v for k, v in body.items() if k != "updatedAt"}
                if create
                else body,
            }
        )
        where_receipt = (
            receipts.c.tenant == self.settings.tenant_id,
            receipts.c.owner == principal.owner,
            receipts.c.actor == principal.actor_id,
            receipts.c.project_id == project_id,
            receipts.c.key == key,
        )
        document_scope = Scope(
            self.settings.tenant_id, principal.owner, "__projects", principal.actor_id
        )
        where = (
            self.repo._scope(documents, document_scope),
            documents.c.kind == "project",
            documents.c.document_id == project_id,
        )
        # Recover an authorized original receipt before resolving today's tier
        # mapping or media. Its immutable request fingerprint remains the wire
        # body, even when a later mapping selects a different deployment.
        if key:
            with self.repo.engine.connect() as conn:
                owner_reference_gate(conn, self.settings.tenant_id, principal.owner)
                assert_project_writable(
                    conn, self.settings.tenant_id, principal.owner, project_id
                )
                prior = (
                    conn.execute(select(receipts).where(*where_receipt))
                    .mappings()
                    .first()
                )
                if prior:
                    if prior["fingerprint"] != fingerprint:
                        raise Conflict("edit_idempotency_conflict")
                    assert_reference_payload(
                        conn,
                        self.settings.tenant_id,
                        principal.owner,
                        prior["response"],
                    )
                    return prior["response"]
        # Resolve immutable source receipts before the document transaction, so
        # no network/filesystem read occupies a database write lock.
        resolved = {}
        generation_actions = {}
        if not create:
            object_fields(
                body,
                {"expected_version", "actions"},
                "操作请求仅接受expected_version和actions",
            )
            if (
                type(body.get("expected_version")) is not int
                or body["expected_version"] < 1
            ):
                raise ValueError("需要当前故事的expected_version")
            actions = body.get("actions")
            if not isinstance(actions, list) or not 1 <= len(actions) <= 200:
                raise ValueError("每次需要1至200个编辑操作")
            for index, action in enumerate(actions):
                validate_action_fields(action)
                if action.get("op", "").startswith("library."):
                    resolved[index] = self.app.state.library.resolve_action(
                        principal, project_id, action
                    )
                elif action.get("op") in {"asset.attach", "artifact.adopt"}:
                    resolved[index] = self.resolve(principal, project_id, action)
                elif action.get("op") == "shot.script_windows.confirm":
                    resolved[index] = self.app.state.script_windows.get(
                        principal, project_id, action["shot_id"], action["preview_id"]
                    )
                elif action.get("op") == "shot.configure_generation":
                    from .generation_draft import prepare_patch
                    from .quality_authoring import normalize

                    selected, _ = normalize(
                        self.settings, self.repo, principal, action, partial=True
                    )
                    generation_actions[index] = selected
                    resolved[index] = prepare_patch(
                        selected,
                        lambda ident: self.resolve(
                            principal,
                            project_id,
                            {"op": "asset.attach", "asset_id": ident},
                        ),
                    )
                elif action.get("op") == "storyboard.defaults":
                    from .quality_authoring import normalize

                    selected, _ = normalize(
                        self.settings,
                        self.repo,
                        principal,
                        action["value"],
                        partial=True,
                    )
                    generation_actions[index] = {**action, "value": selected}
                elif action.get("op") in {"storyboard.restore", "storyboard.star"}:
                    resolved[index] = self.app.state.storyboard_versions.resolve_action(
                        principal, project_id, action
                    )
                elif action.get("op") == "storyboard.select":
                    self.app.state.storyboard_versions.artifact_recipe(
                        principal, project_id, action["shot_id"], action["artifact_id"]
                    )
                    resolved[index] = self.resolve(
                        principal,
                        project_id,
                        {**action, "op": "artifact.adopt", "select": True},
                    )
        # Preview segment-bearing edits only. Resolve their trusted durations
        # before the write lock; the locked CAS repeats the same pure actions
        # and checks the binding against these original source receipts.
        segment_sources = None
        if not create:
            original = self.repo.get_document(document_scope, "project", project_id)
            needs_segments = any(
                has_segments(entity) for entity in original["payload"]["entities"]
            ) or any(
                action["op"] == "shot.segments"
                or any(
                    {"videoSegments", "videoSegmentsSource"}.intersection(value["data"])
                    for value in (action.get("patch"), action.get("entity"))
                    if isinstance(value, dict) and isinstance(value.get("data"), dict)
                )
                for action in body["actions"]
            )
            if needs_segments and original["version"] == body["expected_version"]:
                preview = copy.deepcopy(original["payload"])
                segment_sources = {}
                for index, action in enumerate(body["actions"]):
                    previous = copy.deepcopy(preview)
                    if action["op"] == "shot.segments" and action.get("segments"):
                        shot = entity_by_id(preview, action.get("shot_id"), {"shot"})
                        entity = entity_by_id(
                            preview, shot["data"].get("selectedAssetId"), {"video"}
                        )
                        source = self.resolve_segment_source(
                            principal, project_id, entity
                        )
                        resolved[index] = {
                            "binding": source_binding(entity),
                            "duration": source.get("duration"),
                        }
                    self.apply(
                        preview,
                        generation_actions.get(index, action),
                        resolved.get(index),
                    )
                    validate_segments_transition(previous, preview)
                    segment_sources = self.resolve_segment_sources(
                        principal, project_id, preview, cached=segment_sources
                    )
                    self.check_segment_sources(preview, segment_sources)
        else:
            # Full project creation can carry segment fields too; it must not
            # become a path around trusted source validation.
            segment_sources = self.resolve_segment_sources(principal, project_id, body)
        try:
            with self.repo.transaction() as conn:
                owner_reference_gate(conn, self.settings.tenant_id, principal.owner)
                assert_project_writable(
                    conn, self.settings.tenant_id, principal.owner, project_id
                )
                # The project row serializes concurrent edits. A unique receipt
                # also protects first creation where the project row is absent.
                row = self.repo._locked(conn, select(documents).where(*where))
                if key:
                    receipt = (
                        conn.execute(select(receipts).where(*where_receipt))
                        .mappings()
                        .first()
                    )
                    if receipt:
                        if receipt["fingerprint"] != fingerprint:
                            raise Conflict("edit_idempotency_conflict")
                        assert_reference_payload(
                            conn,
                            self.settings.tenant_id,
                            principal.owner,
                            receipt["response"],
                        )
                        return receipt["response"]
                if create:
                    if row:
                        raise Conflict("document_version_conflict")
                    from .quality_authoring import project as resolve_quality_project

                    project = resolve_quality_project(
                        self.settings, self.repo, principal, body
                    )
                    version = 1
                else:
                    if row is None:
                        raise NotFound("document_not_found")
                    if row["version"] != body["expected_version"]:
                        raise Conflict("document_version_conflict")
                    project = copy.deepcopy(row["payload"])
                    for index, action in enumerate(body["actions"]):
                        before_segments = (
                            copy.deepcopy(project)
                            if segment_sources is not None
                            else None
                        )
                        generic = action["op"] in {
                            "entity.create",
                            "entity.update",
                            "journey.update",
                        }
                        if generic:
                            from .quality_authoring import selector_baseline

                            before_action = selector_baseline(project)
                        self.apply(
                            project,
                            generation_actions.get(index, action),
                            resolved.get(index),
                        )
                        if generic:
                            from .quality_authoring import (
                                project as resolve_quality_project,
                            )

                            project = resolve_quality_project(
                                self.settings,
                                self.repo,
                                principal,
                                project,
                                baseline=before_action,
                            )
                        if before_segments is not None:
                            validate_segments_transition(before_segments, project)
                            self.check_segment_sources(project, segment_sources)
                    from .clip_sound import sync_clip_sound

                    sync_clip_sound(
                        self, conn, principal, project_id, row["payload"], project
                    )
                    version = row["version"] + 1
                if segment_sources is not None:
                    self.check_segment_sources(project, segment_sources)
                project["updatedAt"] = now_iso()
                validate_project(project, self.settings.max_project_bytes)
                assert_reference_payload(
                    conn, self.settings.tenant_id, principal.owner, project
                )
                values = dict(
                    payload=canonical(project),
                    version=version,
                    updated_at=self.repo.clock(),
                )
                if create:
                    conn.execute(
                        insert(documents).values(
                            tenant_id=self.settings.tenant_id,
                            owner_id=principal.owner,
                            project_id="__projects",
                            kind="project",
                            document_id=project_id,
                            **values,
                        )
                    )
                else:
                    conn.execute(update(documents).where(*where).values(**values))
                append_activity(
                    conn,
                    tenant_id=self.settings.tenant_id,
                    principal=principal,
                    project_id=project_id,
                    version=version,
                    occurred_at=values["updated_at"],
                    before=None if create else row["payload"],
                    after=project,
                    actions=() if create else body["actions"],
                    event_type="project.created" if create else "project.edited",
                )
                response = self.envelope({"document_id": project_id, **values})
                if key:
                    conn.execute(
                        insert(receipts).values(
                            tenant=self.settings.tenant_id,
                            owner=principal.owner,
                            actor=principal.actor_id,
                            project_id=project_id,
                            key=key,
                            fingerprint=fingerprint,
                            response=response,
                            created_at=self.repo.clock(),
                        )
                    )
                queue_refresh(principal, project_id)
                return response
        except IntegrityError:
            if key:
                with self.repo.engine.connect() as conn:
                    receipt = (
                        conn.execute(select(receipts).where(*where_receipt))
                        .mappings()
                        .first()
                    )
                    if receipt and receipt["fingerprint"] == fingerprint:
                        return receipt["response"]
            raise Conflict("document_version_conflict") from None

    def resolve(self, principal, project_id, action):
        if action["op"] == "asset.attach":
            if not principal.allows(project_id, "assets:read"):
                raise NotFound("asset_not_found")
            asset_id = action.get("asset_id")
            if not isinstance(asset_id, str):
                raise ValueError("asset_id必须为素材收据ID")
            source = self.app.state.assets.get(principal.owner, asset_id)
            if source["project_id"] != project_id:
                raise NotFound("asset_not_found")
            if source["status"] != "ready":
                raise Conflict("asset_not_ready")
            return {
                "type": source["kind"],
                "title": source["file_name"],
                "data": {
                    "fileId": "cloud_asset_" + asset_id,
                    "cloudAssetId": asset_id,
                    "cloudContentPath": f"/v1/assets/{asset_id}/content",
                    "fileName": source["file_name"],
                    "mime": source["mime"],
                    "metadata": source["metadata"],
                    "source": "upload",
                    "missingFile": False,
                    **(
                        {"sourceSelection": copy.deepcopy(source["selection"])}
                        if isinstance(source.get("selection"), dict)
                        and source["selection"].get("kind") == "artifact_frame"
                        else {}
                    ),
                },
            }
        if not principal.allows(project_id, "jobs:read"):
            raise NotFound("artifact_not_found")
        if not isinstance(action.get("artifact_id"), str) or not ID.fullmatch(
            action["artifact_id"]
        ):
            raise ValueError("artifact_id格式无效")
        with self.repo.engine.connect() as conn:
            row = (
                conn.execute(
                    select(artifacts)
                    .join(jobs, artifacts.c.job_id == jobs.c.id)
                    .where(
                        artifacts.c.id == action.get("artifact_id"),
                        jobs.c.tenant_id == self.settings.tenant_id,
                        jobs.c.owner_id == principal.owner,
                        jobs.c.project_id == project_id,
                        jobs.c.status == "succeeded",
                    )
                )
                .mappings()
                .first()
            )
        if not row:
            raise NotFound("artifact_not_found")
        job = self.repo.get_job_for_owner(
            self.settings.tenant_id, principal.owner, row["job_id"]
        )
        value, ident = row["metadata"], row["id"]
        if value.get("kind") not in {"video", "image", "audio"}:
            raise ValueError("此产物不是可加入素材库的媒体")
        ref = job["request"].get("client_ref", {})
        simulation = (
            job["execution_plan"].get("backend") == "mock"
            or job["request"].get("simulation") is True
        )
        return {
            "type": value["kind"],
            "title": "模拟结果" if simulation else "生成结果",
            "data": {
                "fileId": "cloud_artifact_" + ident,
                "cloudArtifactId": ident,
                "cloudContentPath": f"/v1/artifacts/{ident}/content",
                "fileName": value.get(
                    "filename",
                    ident
                    + {"video": ".mp4", "image": ".png", "audio": ".flac"}[
                        value["kind"]
                    ],
                ),
                "mime": value.get(
                    "mime", value.get("content_type", "application/octet-stream")
                ),
                "bytes": value.get("size_bytes", 0),
                "metadata": {
                    k: v
                    for k, v in value.items()
                    if k not in {"object_key", "provider", "storage_profile"}
                },
                "simulation": simulation,
                "source": "generation",
                "sourceJobId": job["id"],
                "sourceShotId": ref.get("shot_id"),
                "sourceShotVersion": ref.get("shot_version"),
                "sourceHash": ref.get("source_hash"),
                "missingFile": False,
            },
        }

    def resolve_segment_sources(self, principal, project_id, project, *, cached=None):
        """Read canonical source durations outside every document write lock."""
        # Full validation follows locked quality-selector normalization. Before
        # media reads, validate only the structure needed for segment lookups;
        # neutral public quality defaults are not canonical stored defaults yet.
        entities = project.get("entities", [])
        if not isinstance(entities, list) or len(entities) > 5000:
            raise ValueError("项目节点超限")
        seen = set()
        for entity in entities:
            if (
                not isinstance(entity, dict)
                or not isinstance(entity.get("id"), str)
                or not ID.fullmatch(entity["id"])
                or entity["id"] in seen
                or not isinstance(entity.get("type"), str)
                or entity["type"] not in TYPES
                or not isinstance(entity.get("data"), dict)
            ):
                raise ValueError("节点ID、类型或内容无效")
            seen.add(entity["id"])
            validate_segments_data(entity)
        sources = dict(cached or {})
        for shot in entities:
            if not has_segments(shot):
                continue
            entity = entity_by_id(
                project, shot["data"].get("selectedAssetId"), {"video"}
            )
            segment_frames(shot, entity)
            binding = source_binding(entity)
            key = request_hash(binding)
            if key not in sources:
                source = self.resolve_segment_source(principal, project_id, entity)
                duration = source.get("duration")
                if (
                    type(duration) not in (int, float)
                    or not math.isfinite(duration)
                    or duration <= 0
                ):
                    raise ValueError("video_segments_source_duration_unknown")
                sources[key] = duration
            segment_frames(shot, entity, sources[key])
        return sources

    def resolve_segment_source(self, principal, project_id, entity):
        operation = (
            "jobs:read"
            if entity.get("data", {}).get("cloudArtifactId")
            else "assets:read"
        )
        if principal.machine and not principal.allows(project_id, operation):
            raise HTTPException(403, "视频选段需要对应素材读取授权")
        return self.app.state.resolve_render_source(
            principal, project_id, entity, "video"
        )

    @staticmethod
    def check_segment_sources(project, sources):
        for shot in project.get("entities", []):
            if not has_segments(shot):
                continue
            entity = entity_by_id(
                project, shot["data"].get("selectedAssetId"), {"video"}
            )
            key = request_hash(source_binding(entity))
            if key not in sources:
                raise Conflict("video_segments_source_changed")
            ranges = segment_frames(shot, entity, sources[key])
            shot["data"]["videoSegments"] = [
                {"start": value["start"], "end": value["end"]} for value in ranges
            ]

    def apply(self, project, action, resolved=None):
        validate_action_fields(action)
        op = action.get("op")
        if op.startswith("library."):
            from .library import apply_action

            apply_action(project, action, resolved)
        elif op == "storyboard.select":
            self.apply(
                project, {**action, "op": "artifact.adopt", "select": True}, resolved
            )
        elif op.startswith("storyboard."):
            from .storyboard_versions import apply_action

            apply_action(project, action, resolved)
        elif op == "entity.create":
            new_entity(project, action.get("entity"))
        elif op == "entity.update":
            update_entity(
                entity_by_id(project, action.get("entity_id")), action.get("patch")
            )
        elif op == "shot.configure_generation":
            from .generation_draft import configure

            configure(project, action, resolved)
        elif op == "entity.delete":
            delete_entity(
                project, action.get("entity_id"), action.get("cascade", False)
            )
        elif op == "link.create":
            value = object_fields(
                action.get("link"), {"id", "source", "target", "role"}, "连线字段无效"
            )
            project["links"].append({"id": "link-" + uuid.uuid4().hex, **value})
        elif op == "link.delete":
            if not any(l["id"] == action.get("link_id") for l in project["links"]):
                raise NotFound("link_not_found")
            project["links"] = [
                l for l in project["links"] if l["id"] != action["link_id"]
            ]
        elif op == "project.update":
            project.update(
                object_fields(
                    action.get("patch"), {"title", "logline"}, "故事仅可修改标题与简介"
                )
            )
        elif op == "journey.update":
            patch = action.get("patch")
            if not isinstance(patch, dict):
                raise ValueError("journey patch必须为对象")
            # Top-level fields merge; each nested value explicitly replaces its
            # field, exactly like entity.data. No ambiguous deep merge semantics.
            project.setdefault("journey", {}).update(copy.deepcopy(patch))
        elif op == "layout.update":
            patch = object_fields(
                action.get("patch"),
                {"positions", "viewport"},
                "画布仅可修改positions与viewport",
            )
            project["layout"].update(copy.deepcopy(patch))
        elif op in {"asset.attach", "artifact.adopt"}:
            if type(action.get("select", False)) is not bool:
                raise ValueError("select必须为布尔值")
            if action.get("select") and not action.get("shot_id"):
                raise ValueError("采用素材需要shot_id")
            field = "cloudAssetId" if op == "asset.attach" else "cloudArtifactId"
            source_id = resolved["data"][field]
            entity = next(
                (e for e in project["entities"] if e["data"].get(field) == source_id),
                None,
            )
            if entity is None:
                entity = new_entity(
                    project,
                    {
                        **resolved,
                        "id": action.get(
                            "entity_id",
                            ("asset-" if op == "asset.attach" else "result-")
                            + source_id,
                        ),
                        "title": action.get("title", resolved["title"]),
                        "parentId": action.get("parent_id"),
                    },
                )
            elif action.get("entity_id") and action["entity_id"] != entity["id"]:
                raise Conflict("media_already_attached_with_other_entity_id")
            if action.get("shot_id"):
                shot = entity_by_id(project, action["shot_id"], {"shot"})
                candidates = shot["data"].setdefault("candidateIds", [])
                if (
                    entity["type"] in {"video", "image"}
                    and entity["id"] not in candidates
                ):
                    candidates.append(entity["id"])
                if action.get("select"):
                    if entity["type"] not in {"video", "image"}:
                        raise ValueError("镜头候选仅可采用视频或图片")
                    shot["data"]["selectedAssetId"] = entity["id"]
                shot["version"] += 1
                if action.get("role"):
                    project["links"].append(
                        {
                            "id": "link-" + uuid.uuid4().hex,
                            "source": entity["id"],
                            "target": shot["id"],
                            "role": action["role"],
                        }
                    )
        elif op == "shot.select":
            shot = entity_by_id(project, action.get("shot_id"), {"shot"})
            media_id = action.get("entity_id")
            if media_id:
                entity_by_id(project, media_id, {"video", "image"})
            update_entity(shot, {"data": {"selectedAssetId": media_id or ""}})
        elif op == "shot.script_windows.confirm":
            from .script_windows import apply_confirmation

            apply_confirmation(project, action["shot_id"], resolved)
        elif op == "shot.script_windows.clear":
            shot = entity_by_id(project, action["shot_id"], {"shot"})
            shot["data"].pop("script_windows", None)
            shot["version"] += 1
        elif op == "shot.trim":
            shot = entity_by_id(project, action.get("shot_id"), {"shot"})
            entity = entity_by_id(
                project, shot["data"].get("selectedAssetId"), {"video"}
            )
            bound = {
                "assetId": entity["id"],
                **{
                    k: entity["data"].get(k) or None
                    for k in ("fileId", "cloudAssetId", "cloudArtifactId")
                },
                "start": action.get("start"),
                "end": action.get("end"),
            }
            update_entity(shot, {"data": {"selectedVideoRange": bound}})
        elif op == "shot.segments":
            shot = entity_by_id(project, action.get("shot_id"), {"shot"})
            segments = action.get("segments")
            if not isinstance(segments, list):
                raise ValueError("video_segments_invalid")
            if segments:
                entity = entity_by_id(
                    project, shot["data"].get("selectedAssetId"), {"video"}
                )
                if not resolved or resolved.get("binding") != source_binding(entity):
                    raise Conflict("video_segments_source_changed")
                if resolved.get("duration") is None:
                    raise ValueError("video_segments_source_duration_unknown")
                update_entity(
                    shot,
                    {
                        "data": {
                            "videoSegments": copy.deepcopy(segments),
                            "videoSegmentsSource": source_binding(entity),
                        }
                    },
                )
                ranges = segment_frames(shot, entity, resolved["duration"])
                shot["data"]["videoSegments"] = [
                    {"start": value["start"], "end": value["end"]} for value in ranges
                ]
            else:
                update_entity(shot, {"data": {"videoSegments": []}})
                shot["data"].pop("videoSegmentsSource", None)
        elif op == "sound.generated":
            shot = entity_by_id(project, action.get("shot_id"), {"shot"})
            scene = entity_by_id(project, shot["parentId"], {"scene"})
            video = entity_by_id(
                project, shot["data"].get("selectedAssetId"), {"video"}
            )
            audio = [
                e
                for e in project["entities"]
                if e["type"] == "audio"
                and e["data"].get("cloudArtifactId")
                and e["data"].get("sourceJobId") == video["data"].get("sourceJobId")
                and e["data"].get("mime") == "audio/flac"
            ]
            if (
                not video["data"].get("cloudArtifactId")
                or not video["data"].get("sourceJobId")
                or len(audio) != 1
            ):
                raise ValueError(
                    "先加入同一已完成任务的视频和唯一独立FLAC，再关联生成声音"
                )
            audio = audio[0]
            gain = action.get("gain", 0.7)
            if type(gain) not in (float, int) or not 0 <= gain <= 1:
                raise ValueError("音量须为0至1")
            tracks = (
                project.setdefault("journey", {})
                .setdefault("soundTracks", {})
                .setdefault(scene["parentId"], [])
            )
            if any(
                not t.get("generatedFrom")
                and not t.get("muted")
                and t.get("assetId") == audio["id"]
                for t in tracks
            ):
                raise Conflict("generated_audio_already_used_by_manual_track")
            existing = next(
                (
                    t
                    for t in tracks
                    if t.get("generatedFrom")
                    and t.get("shotId") == shot["id"]
                    and not t.get("muted")
                ),
                None,
            )
            start = shot["data"].get("selectedVideoRange", {}).get("start", 0)
            duration = shot["data"].get("seconds", 5)
            track = {
                "id": existing["id"] if existing else "sound-" + uuid.uuid4().hex,
                "assetId": audio["id"],
                "fileId": audio["data"].get("fileId"),
                "shotId": shot["id"],
                "role": "generated",
                "offset": 0,
                "start": start,
                "end": start + duration,
                "duration": duration,
                "gain": gain,
                "muted": False,
                "needsReview": False,
                "generatedFrom": {
                    "jobId": video["data"]["sourceJobId"],
                    "videoEntityId": video["id"],
                    "videoArtifactId": video["data"]["cloudArtifactId"],
                    "audioArtifactId": audio["data"]["cloudArtifactId"],
                },
            }
            if existing:
                tracks[tracks.index(existing)] = track
            else:
                tracks.append(track)
        elif op in {"captions.set", "captions.confirm", "sound.set"}:
            chapter = entity_by_id(project, action.get("chapter_id"), {"chapter"})
            journey = project.setdefault("journey", {})
            if op == "sound.set":
                if (
                    not isinstance(action.get("tracks"), list)
                    or len(action["tracks"]) > MAX_AUDIO_TRACKS
                ):
                    raise ValueError(f"声音轨道须为最多{MAX_AUDIO_TRACKS}项的数组")
                journey.setdefault("soundTracks", {})[chapter["id"]] = copy.deepcopy(
                    action["tracks"]
                )
                if "mode" in action:
                    if action["mode"] not in {"silent", "dialogue", "music", "mixed"}:
                        raise ValueError("声音模式无效")
                    journey.setdefault("sound", {})["mode"] = action["mode"]
            elif op == "captions.set":
                journey.setdefault("captionTracks", {})[chapter["id"]] = {
                    "cues": copy.deepcopy(action.get("cues"))
                }
            else:
                if action.get("reviewed") is not True:
                    raise ValueError("请明确核对字幕正文与时间线后设置reviewed=true")
                validate_project(project, self.settings.max_project_bytes)
                shots, _ = checked_captions(project, chapter["id"])
                track = journey.setdefault("captionTracks", {}).setdefault(
                    chapter["id"], {}
                )
                track["confirmedSnapshot"] = caption_signature(
                    project, chapter["id"], shots
                )
                track["confirmedAt"] = now_iso()


def register_routes(app):
    """Compatibility entry preserving service setup and activity route order."""
    from .guided_routes import create_guided_router

    service = Guided(app)
    app.state.guided = service
    register_activity_routes(app)
    app.include_router(create_guided_router(app, service))
