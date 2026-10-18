"""Owner-scoped reversible authoring removal on the original document authority.

No schema initialization, storage I/O, task creation, automatic expiry or settlement.
"""

import copy
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, StrictBool
from sqlalchemy import select, insert, update, inspect
from .repository import (
    documents,
    jobs,
    attempts,
    registered_workers,
    Scope,
    Conflict,
    NotFound,
    request_hash,
)
from .content_lifecycle import owner_reference_gate, assert_reference_payload

TRASH_SCOPE = "__trash"
TERMINAL = {"succeeded", "failed", "cancelled"}
KINDS = {"project", "entity", "look", "location_version", "asset", "artifact", "take"}


class TrashPreviewTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal[
        "project", "entity", "look", "location_version", "asset", "artifact", "take"
    ]
    project_id: StrictStr = Field(min_length=1)
    id: StrictStr = Field(min_length=1)
    parent_id: StrictStr | None = None
    expected_version: StrictInt | None = Field(default=None, ge=1)


class TrashActionTarget(TrashPreviewTarget):
    expected_version: StrictInt = Field(ge=1)


class TrashPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["trash", "restore", "purge"]
    items: list[TrashPreviewTarget] = Field(min_length=1)
    expected_project_versions: dict[StrictStr, StrictInt] = Field(default_factory=dict)
    confirmed: StrictBool = False


class TrashActionRequest(TrashPreviewRequest):
    items: list[TrashActionTarget] = Field(min_length=1)


def where(tenant, owner, kind, ident=None):
    values = [
        documents.c.tenant_id == tenant,
        documents.c.owner_id == owner,
        documents.c.project_id == TRASH_SCOPE,
        documents.c.kind == kind,
    ]
    if ident is not None:
        values.append(documents.c.document_id == ident)
    return values


def target_key(target):
    return request_hash(
        {k: target.get(k) for k in ("kind", "project_id", "id", "parent_id")}
    )


def active_target(conn, tenant, owner, target):
    if not inspect(conn).has_table(documents.name):
        return None
    row = (
        conn.execute(
            select(documents).where(
                *where(tenant, owner, "trash_item", target_key(target))
            )
        )
        .mappings()
        .first()
    )
    return dict(row) if row and row["payload"]["state"] != "active" else None


def assert_authoring_available(conn, tenant, owner, value):
    """Prevent a stale whole-project save from resurrecting a removed identity."""
    if (
        not isinstance(value, dict)
        or not isinstance(value.get("entities"), list)
        or not isinstance(value.get("id"), str)
    ):
        return
    if not inspect(conn).has_table(documents.name):
        return
    entities = {e.get("id"): e for e in value["entities"] if isinstance(e, dict)}
    for payload in conn.execute(
        select(documents.c.payload).where(
            *where(tenant, owner, "trash_item"),
            documents.c.payload["state"]
            .as_string()
            .in_(("trashed", "purging", "purged")),
        )
    ).scalars():
        target = payload["target"]
        if (
            set(payload.get("removed_entity_ids", {}).get(value["id"], []))
            & entities.keys()
        ):
            raise Conflict("entity_in_trash")
        if target["project_id"] != value["id"]:
            continue
        if target["kind"] == "project":
            raise Conflict("project_in_trash")
        if target["kind"] == "entity" and target["id"] in entities:
            raise Conflict("entity_in_trash")
        if target["kind"] in {"look", "location_version"}:
            parent = entities.get(target.get("parent_id"), {})
            field = "looks" if target["kind"] == "look" else "versions"
            if any(
                v.get("id") == target["id"]
                for v in parent.get("data", {}).get(field, [])
            ):
                raise Conflict("entity_in_trash")


def assert_take_active(conn, tenant, owner, session_id, item_id):
    from .quick_chat import objects

    project = conn.execute(
        select(objects.c.payload["project_id"].as_string()).where(
            objects.c.tenant == tenant,
            objects.c.owner == owner,
            objects.c.kind == "session",
            objects.c.id == session_id,
        )
    ).scalar_one()
    if active_target(
        conn,
        tenant,
        owner,
        {"kind": "take", "project_id": project, "parent_id": session_id, "id": item_id},
    ):
        raise Conflict("take_in_trash")


def project_blocker(conn, tenant, owner, project_id):
    # Original blocker covers collection/upload and authoring unknowns as well
    # as jobs, attempts and leases. Ordinary projects have no synthetic session.
    from types import SimpleNamespace
    from .quick_chat import objects
    from .content_references import history_blocker

    session = (
        conn.execute(
            select(objects).where(
                objects.c.tenant == tenant,
                objects.c.owner == owner,
                objects.c.kind == "session",
                objects.c.payload["project_id"].as_string() == project_id,
            )
        )
        .mappings()
        .first()
    )
    original = (
        dict(session)
        if session
        else {"id": "__no_session__", "payload": {"project_id": project_id}}
    )
    return history_blocker(conn, SimpleNamespace(tenant=tenant), owner, original)


def restore_value(before, after, current):
    """Three-way inverse: preserve unrelated edits; never overwrite a changed reference."""
    if before == after:
        return copy.deepcopy(current)
    if current == after:
        return copy.deepcopy(before)
    if all(isinstance(v, dict) for v in (before, after, current)):
        result = copy.deepcopy(current)
        for key in set(before) | set(after):
            if key == "version":
                continue
            if key in before and key in after:
                if key not in current:
                    if before[key] != after[key]:
                        raise Conflict("trash_restore_reference_conflict")
                else:
                    result[key] = restore_value(before[key], after[key], current[key])
            elif key in before:
                if key in current:
                    raise Conflict("trash_restore_reference_conflict")
                result[key] = copy.deepcopy(before[key])
            elif key in current:
                if current[key] != after[key]:
                    raise Conflict("trash_restore_reference_conflict")
                del result[key]
        return result
    # Entity/link arrays are identity maps, so unrelated additions survive undo.
    if all(
        isinstance(v, list)
        and all(isinstance(i, dict) and isinstance(i.get("id"), str) for i in v)
        for v in (before, after, current)
    ):
        maps = [{i["id"]: i for i in v} for v in (before, after, current)]
        if any(len(m) != len(v) for m, v in zip(maps, (before, after, current))):
            raise Conflict("trash_restore_reference_conflict")
        merged = restore_value(*maps)
        order = [i["id"] for i in current] + [
            i["id"] for i in before if i["id"] not in maps[2]
        ]
        return [merged[i] for i in order if i in merged]
    raise Conflict("trash_restore_reference_conflict")


def remove_authoring(project, target):
    from .guided import delete_entity, entity_by_id

    before = copy.deepcopy(project)
    if target["kind"] == "entity":
        delete_entity(project, target["id"], True)
    elif target["kind"] in {"look", "location_version"}:
        entity = entity_by_id(
            project,
            target["parent_id"],
            {"character" if target["kind"] == "look" else "location"},
        )
        field, default = (
            ("looks", "defaultLookId")
            if target["kind"] == "look"
            else ("versions", "defaultVersionId")
        )
        values = entity["data"].get(field, [])
        if not any(v["id"] == target["id"] for v in values):
            raise NotFound("trash_target_not_found")
        effective_default = entity["data"].get(default) or (
            values[0]["id"] if values else None
        )
        removed_default = effective_default == target["id"]
        entity["data"][field] = [v for v in values if v["id"] != target["id"]]
        if entity["data"].get(default) == target["id"]:
            entity["data"][default] = ""
        entity["version"] += 1
        for item in project["entities"]:
            data = item["data"]
            before_data = copy.deepcopy(data)
            if field == "looks":

                def removed_binding(binding):
                    character = (
                        binding
                        if isinstance(binding, str)
                        else binding.get("characterId")
                    )
                    selected = "" if isinstance(binding, str) else binding.get("lookId")
                    return character == entity["id"] and (
                        selected == target["id"] or not selected and removed_default
                    )

                if "cast" in data:
                    data["cast"] = [
                        binding
                        for binding in data["cast"]
                        if not removed_binding(binding)
                    ]
            else:
                if removed_default and data.get("locationId") == entity["id"]:
                    data["locationId"] = ""
                place = data.get("storyboard", {}).get("place")
                if (
                    place
                    and place.get("locationId") == entity["id"]
                    and (
                        place.get("versionId") == target["id"]
                        or not place.get("versionId")
                        and removed_default
                    )
                ):
                    data["storyboard"]["place"] = None
            if data != before_data:
                item["version"] += 1

    else:
        raise ValueError("Unsupported authoring target / 不支持的编辑对象")
    # Preserve at least one originally existing chapter and scene per container.
    for kind in ("chapter", "scene"):
        parents = {e.get("parentId") for e in before["entities"] if e["type"] == kind}
        remaining_ids = {e["id"] for e in project["entities"]}
        for parent in parents:
            if parent is not None and parent not in remaining_ids:
                continue
            if not any(
                e["type"] == kind and e.get("parentId") == parent
                for e in project["entities"]
            ):
                raise Conflict("trash_last_container_required")
    return before


class UniversalTrash:
    def __init__(self, app):
        self.app, self.repo, self.settings = (
            app,
            app.state.repository,
            app.state.settings,
        )
        self.tenant = self.settings.tenant_id

    def _project(self, conn, principal, ident, operation="projects:write"):
        if not principal.allows(ident, operation):
            raise NotFound("trash_target_not_found")
        row = (
            conn.execute(
                select(documents).where(
                    documents.c.tenant_id == self.tenant,
                    documents.c.owner_id == principal.owner,
                    documents.c.project_id == "__projects",
                    documents.c.kind == "project",
                    documents.c.document_id == ident,
                )
            )
            .mappings()
            .first()
        )
        if not row or row["payload"].get("content_deleted"):
            raise NotFound("trash_target_not_found")
        return dict(row)

    def _store(self, conn, owner, kind, ident, payload, old=None):
        fields = dict(
            payload=payload,
            version=(old["version"] + 1 if old else 1),
            updated_at=self.repo.clock(),
        )
        if old:
            conn.execute(
                update(documents)
                .where(*where(self.tenant, owner, kind, ident))
                .values(**fields)
            )
        else:
            conn.execute(
                insert(documents).values(
                    tenant_id=self.tenant,
                    owner_id=owner,
                    project_id=TRASH_SCOPE,
                    kind=kind,
                    document_id=ident,
                    **fields,
                )
            )
        return {"document_id": ident, **fields}

    def public(self, row):
        p = row["payload"]
        return {
            "trash_id": row["document_id"],
            "version": row["version"],
            "target": p["target"],
            "state": p["state"],
            "trashed_at": p.get("trashed_at"),
            "purge_at": None,
            "auto_purge": False,
            "title": p.get("title", ""),
            "kind": p.get("display_kind", p["target"]["kind"]),
            "retained_media": p.get("retained_media", True),
        }

    def list(self, principal, *, kind=None, limit=100, offset=0):
        if (
            type(limit) is not int
            or not 1 <= limit <= 100
            or type(offset) is not int
            or offset < 0
        ):
            raise ValueError("Invalid page / 分页无效")
        if principal.machine and "projects:read" not in principal.scopes:
            return {"items": [], "next_offset": None, "auto_purge": False}
        columns = [documents.c.document_id, documents.c.version]
        columns += [
            documents.c.payload[name].label(name)
            for name in (
                "target",
                "state",
                "trashed_at",
                "title",
                "display_kind",
                "retained_media",
            )
        ]
        query = select(*columns).where(
            *where(self.tenant, principal.owner, "trash_item"),
            documents.c.payload["state"].as_string().in_(("trashed", "purging")),
        )
        if principal.machine and not principal.all_projects:
            query = query.where(
                documents.c.payload["target"]["project_id"]
                .as_string()
                .in_(principal.project_ids)
            )
        if kind is not None:
            query = query.where(documents.c.payload["display_kind"].as_string() == kind)
        with self.repo.engine.connect() as conn:
            rows = list(
                conn.execute(
                    query.order_by(
                        documents.c.updated_at.desc(), documents.c.document_id
                    )
                    .limit(limit + 1)
                    .offset(offset)
                ).mappings()
            )
        return {
            "items": [
                self.public(
                    {
                        "document_id": r["document_id"],
                        "version": r["version"],
                        "payload": {
                            k: v
                            for k, v in r.items()
                            if k not in {"document_id", "version"}
                        },
                    }
                )
                for r in rows[:limit]
            ],
            "next_offset": offset + limit if len(rows) > limit else None,
            "auto_purge": False,
        }

    def _projects(self, conn, principal):
        return [
            dict(r)
            for r in conn.execute(
                select(documents).where(
                    documents.c.tenant_id == self.tenant,
                    documents.c.owner_id == principal.owner,
                    documents.c.project_id == "__projects",
                    documents.c.kind == "project",
                )
            ).mappings()
            if not r["payload"].get("content_deleted")
        ]

    def _check_target(self, conn, principal, target):
        if not isinstance(target, dict) or set(target) - {
            "kind",
            "project_id",
            "id",
            "parent_id",
            "expected_version",
        }:
            raise ValueError("Invalid trash target / 回收对象无效")
        if (
            not isinstance(target.get("kind"), str)
            or target.get("kind") not in KINDS
            or not all(
                isinstance(target.get(k), str) and target[k]
                for k in ("project_id", "id")
            )
        ):
            raise ValueError("Invalid trash identity / 回收对象身份无效")
        if (
            type(target.get("expected_version")) is not int
            or target["expected_version"] < 1
        ):
            raise ValueError("Expected version is required / 需要当前版本")
        if not principal.allows(target["project_id"], "projects:write"):
            raise NotFound("trash_target_not_found")
        kind = target["kind"]
        if kind in {"look", "location_version", "take"} and (
            not isinstance(target.get("parent_id"), str) or not target["parent_id"]
        ):
            raise ValueError("Parent identity is required / 需要父对象身份")
        record = self._project(conn, principal, target["project_id"])
        if kind == "take":
            session = self.app.state.quick_chat._access(
                principal, target.get("parent_id"), "projects:write", conn=conn
            )
            if session["payload"]["project_id"] != target["project_id"]:
                raise NotFound("trash_target_not_found")
            self.app.state.quick_chat._take(
                conn, principal, target["parent_id"], target["id"]
            )
        elif kind not in {"asset", "artifact"}:
            from .generation_admission import reject_managed

            reject_managed(record["payload"])
        if kind == "asset":
            from .assets import asset_table

            row = (
                conn.execute(
                    select(asset_table).where(
                        asset_table.c.tenant == self.tenant,
                        asset_table.c.owner == principal.owner,
                        asset_table.c.id == target["id"],
                        asset_table.c.project_id == target["project_id"],
                    )
                )
                .mappings()
                .first()
            )
            if not row or not principal.allows(target["project_id"], "assets:write"):
                raise NotFound("trash_target_not_found")
            if row["status"] not in {"ready", "duplicate"}:
                raise Conflict("trash_upload_unresolved")
        elif kind == "artifact":
            from .repository import artifacts

            row = conn.execute(
                select(jobs.c.project_id)
                .select_from(artifacts.join(jobs, jobs.c.id == artifacts.c.job_id))
                .where(
                    artifacts.c.id == target["id"],
                    jobs.c.tenant_id == self.tenant,
                    jobs.c.owner_id == principal.owner,
                )
            ).first()
            if (
                not row
                or row[0] != target["project_id"]
                or not principal.allows(row[0], "jobs:read")
            ):
                raise NotFound("trash_target_not_found")
        return {k: v for k, v in target.items() if k != "expected_version"}

    def _describe(self, conn, principal, target):
        record = self._project(conn, principal, target["project_id"])
        project = record["payload"]
        kind = target["kind"]
        if kind == "project":
            if target["id"] != target["project_id"]:
                raise NotFound("trash_target_not_found")
            return project.get("title", ""), "project"
        if kind == "take":
            revision, item = self.app.state.quick_chat._take(
                conn, principal, target["parent_id"], target["id"]
            )
            return revision["payload"].get("title", ""), "take"
        if kind == "entity":
            from .guided import entity_by_id

            entity = entity_by_id(project, target["id"])
            return entity["title"], entity["type"]
        if kind in {"look", "location_version"}:
            from .guided import entity_by_id

            entity = entity_by_id(project, target.get("parent_id"))
            field = "looks" if kind == "look" else "versions"
            item = next(
                (v for v in entity["data"].get(field, []) if v["id"] == target["id"]),
                None,
            )
            if item is None:
                raise NotFound("trash_target_not_found")
            return item.get("name", ""), kind
        if kind == "asset":
            import json
            from .assets import asset_table

            value = conn.execute(
                select(asset_table.c.record).where(
                    asset_table.c.id == target["id"],
                    asset_table.c.tenant == self.tenant,
                    asset_table.c.owner == principal.owner,
                )
            ).scalar_one()
            return json.loads(value).get("file_name", ""), kind
        if kind == "artifact":
            entity = next(
                (
                    e
                    for e in project.get("entities", [])
                    if e.get("data", {}).get("cloudArtifactId") == target["id"]
                ),
                None,
            )
            return (entity.get("title", "") if entity else target["id"]), kind
        return "", kind

    def _soft(self, conn, principal, target, versions, staged):
        from .guided import delete_entity

        title, display = self._describe(conn, principal, target)
        projects = {}
        kind = target["kind"]
        original = self._project(conn, principal, target["project_id"])
        if active_target(
            conn,
            self.tenant,
            principal.owner,
            {
                "kind": "project",
                "project_id": target["project_id"],
                "id": target["project_id"],
            },
        ):
            raise Conflict("project_in_trash")
        if target["project_id"] in staged:
            original["payload"] = staged[target["project_id"]]
        if original["version"] != versions.get(target["project_id"]):
            raise Conflict("trash_project_version_conflict")
        blocker = project_blocker(
            conn, self.tenant, principal.owner, target["project_id"]
        )
        if blocker:
            raise Conflict(blocker)
        if kind in {"entity", "look", "location_version"}:
            value = copy.deepcopy(original["payload"])
            before = remove_authoring(value, target)
            projects[target["project_id"]] = {"before": before, "after": value}
        elif kind in {"asset", "artifact"}:
            from .content_lifecycle import reference_ids

            for job in conn.execute(
                select(jobs).where(
                    jobs.c.tenant_id == self.tenant, jobs.c.owner_id == principal.owner
                )
            ).mappings():
                a, b = reference_ids(
                    {"request": job["request"], "execution": job["execution_plan"]}
                )
                if target["id"] in (a if kind == "asset" else b):
                    reason = project_blocker(
                        conn, self.tenant, principal.owner, job["project_id"]
                    )
                    if reason:
                        raise Conflict(reason)
            field = "cloudAssetId" if kind == "asset" else "cloudArtifactId"
            for row in self._projects(conn, principal):
                if row["document_id"] in staged:
                    row["payload"] = staged[row["document_id"]]
                matches = [
                    e["id"]
                    for e in row["payload"].get("entities", [])
                    if e.get("data", {}).get(field) == target["id"]
                ]
                if not matches:
                    continue
                if not principal.allows(row["document_id"], "projects:write"):
                    raise Conflict("trash_reference_scope_required")
                if row["payload"].get("integration_kind") == "quick_chat":
                    # Immutable per-take projections retain their source binding.
                    # The asset availability fence prevents any new use.
                    continue
                if row["version"] != versions.get(row["document_id"]):
                    raise Conflict("trash_project_version_conflict")
                blocker = project_blocker(
                    conn, self.tenant, principal.owner, row["document_id"]
                )
                if blocker:
                    raise Conflict(blocker)
                value = copy.deepcopy(row["payload"])
                for ident in matches:
                    delete_entity(value, ident, True)
                projects[row["document_id"]] = {
                    "before": row["payload"],
                    "after": value,
                }
        elif kind not in {"project", "take"}:
            raise ValueError("Unsupported target / 不支持的对象")
        return {
            "target": target,
            "state": "trashed",
            "trashed_at": self.repo.clock(),
            "title": title,
            "display_kind": display,
            "projects": projects,
            "retained_media": True,
            "removed_entity_ids": {
                p: sorted(
                    {e["id"] for e in c["before"]["entities"]}
                    - {e["id"] for e in c["after"]["entities"]}
                )
                for p, c in projects.items()
            },
        }

    def _write_projects(self, conn, principal, changes):
        from .project_validation import validate_project

        for ident, project in changes.items():
            row = self._project(conn, principal, ident)
            validate_project(project, self.settings.max_project_bytes)
            assert_reference_payload(conn, self.tenant, principal.owner, project)
            conn.execute(
                update(documents)
                .where(
                    documents.c.tenant_id == self.tenant,
                    documents.c.owner_id == principal.owner,
                    documents.c.project_id == "__projects",
                    documents.c.kind == "project",
                    documents.c.document_id == ident,
                )
                .values(
                    payload=project,
                    version=row["version"] + 1,
                    updated_at=self.repo.clock(),
                )
            )

    def _response(self, row):
        return {"operation_id": row["document_id"], **row["payload"]["response"]}

    def get_action(self, principal, ident):
        with self.repo.engine.connect() as conn:
            row = (
                conn.execute(
                    select(documents).where(
                        *where(self.tenant, principal.owner, "trash_action", ident)
                    )
                )
                .mappings()
                .first()
            )
            if not row or any(
                not principal.allows(t["project_id"], "projects:read")
                for t in row["payload"]["targets"]
            ):
                raise NotFound("trash_operation_not_found")
            return self._response(row)

    def _purge_plan(self, conn, principal, pending):
        from .content_lifecycle import reference_ids, assert_history_cleanup_activated
        from .content_references import cleanup_plan
        from .assets import asset_table
        from .repository import artifacts

        assert_history_cleanup_activated(conn, self.tenant)
        asset_ids, artifact_ids = set(), set()
        projects = []
        ignored = []
        for row, payload in pending:
            target = payload["target"]
            ignored.append(target_key(target))
            for capsule in payload.get("projects", {}).values():
                before_a, before_b = reference_ids(capsule["before"])
                after_a, after_b = reference_ids(capsule["after"])
                asset_ids.update(before_a - after_a)
                artifact_ids.update(before_b - after_b)
            if target["kind"] == "asset":
                asset_ids.add(target["id"])
            if target["kind"] == "artifact":
                original = (
                    conn.execute(
                        select(artifacts).where(artifacts.c.id == target["id"])
                    )
                    .mappings()
                    .one()
                )
                artifact_ids.update(
                    conn.execute(
                        select(artifacts.c.id).where(
                            artifacts.c.job_id == original["job_id"],
                            artifacts.c.attempt_id == original["attempt_id"],
                        )
                    ).scalars()
                )
            if target["kind"] == "take":
                from .quick_chat import objects

                ownjobs = list(
                    conn.execute(
                        select(objects.c.payload["job_id"].as_string()).where(
                            objects.c.tenant == self.tenant,
                            objects.c.owner == principal.owner,
                            objects.c.session_id == target["parent_id"],
                            objects.c.kind == "execution",
                            objects.c.payload["item_id"].as_string() == target["id"],
                        )
                    ).scalars()
                )
                artifact_ids.update(
                    conn.execute(
                        select(artifacts.c.id).where(
                            artifacts.c.job_id.in_([j for j in ownjobs if j])
                        )
                    ).scalars()
                )
            if target["kind"] == "project":
                projects.append(target["project_id"])
                asset_ids.update(
                    conn.execute(
                        select(asset_table.c.id).where(
                            asset_table.c.tenant == self.tenant,
                            asset_table.c.owner == principal.owner,
                            asset_table.c.project_id == target["project_id"],
                        )
                    ).scalars()
                )
                artifact_ids.update(
                    conn.execute(
                        select(artifacts.c.id)
                        .join(jobs, jobs.c.id == artifacts.c.job_id)
                        .where(
                            jobs.c.tenant_id == self.tenant,
                            jobs.c.owner_id == principal.owner,
                            jobs.c.project_id == target["project_id"],
                        )
                    ).scalars()
                )
        return cleanup_plan(
            conn,
            self.app.state.quick_chat,
            principal,
            [],
            media_targets={"asset_ids": asset_ids, "artifact_ids": artifact_ids},
            ignored_trash_ids=ignored,
            purged_projects=projects,
        )

    def apply(self, principal, body, key, *, preview=False):
        from .quick_chat import key_check

        key_check(key)
        if not isinstance(body, dict) or set(body) - {
            "action",
            "items",
            "expected_project_versions",
            "confirmed",
        }:
            raise ValueError("Invalid trash request / 回收请求无效")
        action = body.get("action")
        if (
            not isinstance(action, str)
            or action not in {"trash", "restore", "purge"}
            or not isinstance(body.get("items"), list)
            or not body["items"]
        ):
            raise ValueError("Select items and action / 请选择对象和操作")
        if action == "purge" and not preview and body.get("confirmed") is not True:
            raise Conflict("trash_permanent_confirmation_required")
        if (
            len(__import__('json').dumps(body).encode())
            > self.settings.max_project_bytes
        ):
            raise ValueError(
                "Request exceeds configured document size / 请求超过文档大小设置"
            )
        ident = request_hash(
            {
                "tenant": self.tenant,
                "owner": principal.owner,
                "actor": principal.actor_id,
                "key": key,
            }
        )
        fingerprint = request_hash(body)
        if action == "purge" and not preview:
            # Establish owner/scope/closed eligibility before the original quota
            # backfill. The final transaction rechecks every version and blocker.
            with self.repo.engine.connect() as read:
                prior = (
                    read.execute(
                        select(documents).where(
                            *where(self.tenant, principal.owner, "trash_action", ident)
                        )
                    )
                    .mappings()
                    .first()
                )
            if prior:
                if prior["payload"]["request_hash"] != fingerprint:
                    raise Conflict("trash_idempotency_conflict")
                return self.get_action(principal, ident)
            self.apply(principal, body, key, preview=True)
            self.app.state.quick_chat.assets.usage(principal.owner)
        with self.repo.transaction() as conn:
            owner_reference_gate(conn, self.tenant, principal.owner)
            old = (
                conn.execute(
                    select(documents).where(
                        *where(self.tenant, principal.owner, "trash_action", ident)
                    )
                )
                .mappings()
                .first()
            )
            if old and not preview:
                if old["payload"]["request_hash"] != fingerprint:
                    raise Conflict("trash_idempotency_conflict")
                for t in old["payload"]["targets"]:
                    if not principal.allows(t["project_id"], "projects:write"):
                        raise NotFound("trash_operation_not_found")
                return self._response(old)
            supplied_items = copy.deepcopy(body["items"])
            if preview:
                for supplied in supplied_items:
                    if "expected_version" not in supplied:
                        if action == "trash":
                            supplied["expected_version"] = self._project(
                                conn, principal, supplied.get("project_id")
                            )["version"]
                        else:
                            previous = active_target(
                                conn, self.tenant, principal.owner, supplied
                            )
                            if not previous:
                                raise NotFound("trash_target_not_found")
                            supplied["expected_version"] = previous["version"]
            targets = [self._check_target(conn, principal, t) for t in supplied_items]
            if action == "purge":
                from .quick_chat_history import CLEANUP_SCOPES

                if any(
                    not principal.allows(t["project_id"], scope)
                    for t in targets
                    for scope in CLEANUP_SCOPES
                ):
                    raise Conflict("trash_cleanup_scope_required")
                for target in targets:
                    if target["kind"] == "project":
                        dependents = conn.execute(
                            select(documents.c.document_id).where(
                                documents.c.tenant_id == self.tenant,
                                documents.c.owner_id == principal.owner,
                                documents.c.kind == "trash_item",
                                documents.c.project_id == "__trash",
                                documents.c.payload["state"]
                                .as_string()
                                .in_(("trashed", "purging")),
                                documents.c.payload["target"]["project_id"].as_string()
                                == target["project_id"],
                                documents.c.document_id != target_key(target),
                            )
                        ).first()
                        if dependents:
                            raise Conflict("trash_dependent_items_retained")
            if len({target_key(t) for t in targets}) != len(targets):
                raise ValueError("Duplicate targets / 对象重复")
            if "expected_project_versions" in body and not isinstance(
                body["expected_project_versions"], dict
            ):
                raise ValueError("Invalid project versions / 故事版本无效")
            versions = dict(body.get("expected_project_versions") or {})
            if any(type(v) is not int or v < 1 for v in versions.values()):
                raise ValueError("Invalid project versions / 故事版本无效")
            if preview:
                versions.update(
                    {
                        r["document_id"]: r["version"]
                        for r in self._projects(conn, principal)
                        if principal.allows(r["document_id"], "projects:write")
                    }
                )
            if action == "trash":
                for supplied in supplied_items:
                    p = supplied["project_id"]
                    if p in versions and versions[p] != supplied["expected_version"]:
                        raise Conflict("trash_project_version_conflict")
                    versions[p] = supplied["expected_version"]
            if any(
                t["kind"] == "project"
                and any(
                    v is not t and v["project_id"] == t["project_id"] for v in targets
                )
                for t in targets
            ):
                raise Conflict("trash_overlapping_targets")
            if action == "restore":

                def undo_order(pair):
                    row = active_target(conn, self.tenant, principal.owner, pair[1])
                    payload = row["payload"] if row else {}
                    return (
                        max(payload.get("base_versions", {}).values(), default=0),
                        payload.get("trash_order", 0),
                    )

                pairs = sorted(
                    zip(supplied_items, targets), key=undo_order, reverse=True
                )
                supplied_items, targets = map(list, zip(*pairs))
            pending = []
            changes = {}
            for supplied, target in zip(supplied_items, targets):
                row = (
                    conn.execute(
                        select(documents).where(
                            *where(
                                self.tenant,
                                principal.owner,
                                "trash_item",
                                target_key(target),
                            )
                        )
                    )
                    .mappings()
                    .first()
                )
                if action == "trash":
                    if row and row["payload"]["state"] != "active":
                        raise Conflict("trash_target_not_active")
                    payload = self._soft(conn, principal, target, versions, changes)
                    payload.update(
                        base_versions=dict(versions), trash_order=len(pending)
                    )
                    for p, capsule in payload["projects"].items():
                        changes[p] = capsule["after"]
                else:
                    if not row or row["payload"]["state"] != "trashed":
                        raise Conflict("trash_target_not_trashed")
                    if row["version"] != supplied["expected_version"]:
                        raise Conflict("trash_version_conflict")
                    payload = copy.deepcopy(row["payload"])
                    primary = self._project(conn, principal, target["project_id"])
                    if primary["version"] != versions.get(target["project_id"]):
                        raise Conflict("trash_project_version_conflict")
                    if action == "restore":
                        for p, capsule in payload["projects"].items():
                            current = self._project(conn, principal, p)
                            if current["version"] != versions.get(p):
                                raise Conflict("trash_project_version_conflict")
                            if p in changes:
                                current["payload"] = changes[p]
                            restored = restore_value(
                                capsule["before"], capsule["after"], current["payload"]
                            )
                            live = {e["id"]: e for e in current["payload"]["entities"]}
                            for e in restored["entities"]:
                                if live.get(e["id"]) != e:
                                    e["version"] = (
                                        max(
                                            e["version"],
                                            live.get(e["id"], {}).get("version", 0),
                                        )
                                        + 1
                                    )
                            changes[p] = restored
                        payload.update(state="active", restored_at=self.repo.clock())
                    else:
                        blocker = project_blocker(
                            conn, self.tenant, principal.owner, target["project_id"]
                        )
                        if blocker:
                            raise Conflict(blocker)
                        # Metadata purge is explicit; immutable accepted receipts/media remain protected.
                        payload.update(
                            state="purging", purge_requested_at=self.repo.clock()
                        )
                pending.append((row, payload))
            # Representation bound derives from the existing project-byte setting,
            # not an independent business cap. Split a larger explicit selection.
            if (
                len(
                    __import__('json')
                    .dumps([p for _, p in pending], ensure_ascii=False)
                    .encode()
                )
                > 2 * self.settings.max_project_bytes
            ):
                raise ValueError(
                    "Trash selection exceeds configured project byte capacity; select fewer items. / 回收选择超过项目字节容量，请减少本次选择。"
                )
            purge_plan = (
                self._purge_plan(conn, principal, pending)
                if action == "purge"
                else None
            )
            if preview:
                return {
                    "action": action,
                    "request_hash": fingerprint,
                    "eligible": True,
                    "items": [
                        {
                            "target": p["target"],
                            "kind": p["display_kind"],
                            "title": p["title"],
                            "expected_version": supplied["expected_version"],
                        }
                        for (_, p), supplied in zip(pending, supplied_items)
                    ],
                    "affected_project_versions": {
                        p: self._project(conn, principal, p)["version"]
                        for p in set(changes) | {t["project_id"] for t in targets}
                    },
                    "permanent_confirmation_required": action == "purge",
                    "auto_purge": False,
                    "removed": purge_plan["counts"] if purge_plan else {},
                    "kept": purge_plan["kept"] if purge_plan else {},
                }
            # References are already absent on purge. Restore writes before clearing markers
            # through an explicit authorized path; it never resurrects irreversible tombstones.
            for row, payload in pending:
                self._store(
                    conn,
                    principal.owner,
                    "trash_item",
                    target_key(payload["target"]),
                    payload,
                    row,
                )
            self._write_projects(conn, principal, changes)
            items = [
                self.public(
                    self._store_result(conn, principal, target_key(p["target"]))
                )
                for _, p in pending
            ]
            response = {
                "action": action,
                "state": "pending" if purge_plan else "completed",
                "request_hash": fingerprint,
                "items": items,
                "project_versions": {
                    p: self._project(conn, principal, p)["version"]
                    for p in set(changes) | {t["project_id"] for t in targets}
                },
                "ledger_retained": True,
                "media_deleted": False,
                "auto_purge": False,
            }
            row = self._store(
                conn,
                principal.owner,
                "trash_action",
                ident,
                {"request_hash": fingerprint, "targets": targets, "response": response},
            )
            if purge_plan is not None:
                from .quick_chat import objects
                from .content_lifecycle import put_tombstone

                now = self.repo.clock()
                payload = {
                    "state": "pending",
                    "action": "purge",
                    "operation_id": ident,
                    "trash_ids": [target_key(t) for t in targets],
                    "plan": purge_plan,
                    "accounting_released": False,
                }
                conn.execute(
                    insert(objects).values(
                        id="universal-purge-" + ident,
                        tenant=self.tenant,
                        owner=principal.owner,
                        session_id="__universal_trash",
                        kind="universal_purge",
                        parent_id=None,
                        business_key=ident,
                        version=1,
                        payload=payload,
                        created_at=now,
                        updated_at=now,
                    )
                )
                for kind, ids in (
                    ("asset", purge_plan["asset_ids"]),
                    ("artifact", purge_plan["artifact_ids"]),
                ):
                    for value in ids:
                        put_tombstone(
                            conn,
                            self.tenant,
                            principal.owner,
                            kind=kind,
                            value=value,
                            action_id="universal-purge-" + ident,
                            now=now,
                        )
                for spec in purge_plan["objects"]:
                    for binding in spec["bindings"]:
                        put_tombstone(
                            conn,
                            self.tenant,
                            principal.owner,
                            kind="key",
                            value=spec["object_key"],
                            storage_binding=binding,
                            action_id="universal-purge-" + ident,
                            now=now,
                        )
            return self._response(row)

    def _store_result(self, conn, principal, ident):
        return (
            conn.execute(
                select(documents).where(
                    *where(self.tenant, principal.owner, "trash_item", ident)
                )
            )
            .mappings()
            .one()
        )


def register_routes(app):
    from fastapi import Request, Header, HTTPException

    service = UniversalTrash(app)
    app.state.universal_trash = service

    @app.get("/v1/trash")
    def listing(
        request: Request, kind: str | None = None, limit: int = 100, offset: int = 0
    ):
        return service.list(
            request.state.principal, kind=kind, limit=limit, offset=offset
        )

    def perform(request, body, key, preview):
        try:
            return service.apply(request.state.principal, body, key, preview=preview)
        except Conflict as exc:
            # Every operation here is a single original-owner SQL transaction;
            # known failures roll it back and make no storage/provider calls.
            code = str(exc)
            if code in {
                "trash_project_version_conflict",
                "trash_version_conflict",
                "trash_restore_reference_conflict",
                "trash_last_container_required",
                "jobs_active_or_uncertain",
                "upstream_stop_unconfirmed",
                "worker_obligation_unresolved",
                "admission_unresolved",
                "authoring_operation_unresolved",
                "upload_unresolved",
                "collection_unresolved",
                "job_binding_unknown",
                "history_cleanup_not_enabled",
                "trash_reference_scope_required",
                "trash_target_not_active",
                "trash_target_not_trashed",
                "trash_upload_unresolved",
                "trash_permanent_confirmation_required",
                "project_in_trash",
                "trash_overlapping_targets",
                "trash_cleanup_scope_required",
                "trash_dependent_items_retained",
            }:
                raise HTTPException(
                    409,
                    {
                        "code": code,
                        "resource": "trash",
                        "request_hash": request_hash(body),
                        "no_mutation_committed": True,
                        "message": "No changes committed; reload and review the blocked items. / 未提交更改，请刷新并检查受阻对象。",
                    },
                ) from None
            raise

    @app.post("/v1/trash/preview")
    def preview(request: Request, body: TrashPreviewRequest):
        return perform(request, body.model_dump(exclude_unset=True), "preview", True)

    @app.post("/v1/trash/actions")
    def action(
        request: Request,
        body: TrashActionRequest,
        key: str = Header(..., alias="Idempotency-Key"),
    ):
        return perform(request, body.model_dump(exclude_unset=True), key, False)

    @app.get("/v1/trash/actions/{operation_id}")
    def observe(operation_id: str, request: Request):
        return service.get_action(request.state.principal, operation_id)


def finish_purge(service, record):
    """Finish only proven object deletions; preserve immutable job/attempt/accounting."""
    from .quick_chat import objects
    from .repository import artifacts
    from .content_lifecycle import assert_history_cleanup_activated

    with service.repo.transaction() as conn:
        owner_reference_gate(conn, service.tenant, record["owner"])
        assert_history_cleanup_activated(conn, service.tenant)
        current = (
            conn.execute(select(objects).where(objects.c.id == record["id"]))
            .mappings()
            .one()
        )
        payload = copy.deepcopy(current["payload"])
        if payload["state"] == "completed" or any(
            s["state"] != "deleted" for s in payload["plan"]["objects"]
        ):
            return
        plan = payload["plan"]
        now = service.repo.clock()
        if not payload["accounting_released"]:
            service.assets.finalize_purge(
                conn,
                record["owner"],
                asset_ids=plan["asset_ids"],
                artifact_specs=[
                    s
                    for s in plan["objects"]
                    if s["storage_binding"] != service.assets.storage_binding
                ],
                now=now,
            )
            payload["accounting_released"] = True
        for ident in plan["artifact_ids"]:
            artifact = (
                conn.execute(select(artifacts).where(artifacts.c.id == ident))
                .mappings()
                .one()
            )
            conn.execute(
                update(artifacts)
                .where(artifacts.c.id == ident)
                .values(metadata={**artifact["metadata"], "content_deleted": True})
            )
        final = []
        for ident in payload["trash_ids"]:
            row = (
                conn.execute(
                    select(documents).where(
                        *where(service.tenant, record["owner"], "trash_item", ident)
                    )
                )
                .mappings()
                .one()
            )
            target = row["payload"]["target"]
            if target["kind"] == "project":
                # Explicitly purge authoring content, retaining document and edit
                # receipt identities. Accepted job inputs/financial rows are never edited.
                project_where = (
                    documents.c.tenant_id == service.tenant,
                    documents.c.owner_id == record["owner"],
                    documents.c.project_id == "__projects",
                    documents.c.kind == "project",
                    documents.c.document_id == target["project_id"],
                )
                conn.execute(
                    update(documents)
                    .where(*project_where)
                    .values(
                        payload={
                            "id": target["project_id"],
                            "title": "",
                            "content_deleted": True,
                        },
                        version=documents.c.version + 1,
                        updated_at=now,
                    )
                )
            value = {
                **row["payload"],
                "state": "purged",
                "purged_at": now,
                "projects": {},
                "retained_media": bool(plan["kept"]["objects"]),
            }
            conn.execute(
                update(documents)
                .where(*where(service.tenant, record["owner"], "trash_item", ident))
                .values(payload=value, version=row["version"] + 1, updated_at=now)
            )
            final.append(
                {
                    "trash_id": ident,
                    "version": row["version"] + 1,
                    "state": "purged",
                    "target": value["target"],
                    "title": value.get("title", ""),
                    "kind": value["display_kind"],
                    "trashed_at": value["trashed_at"],
                    "purge_at": None,
                    "auto_purge": False,
                    "retained_media": value["retained_media"],
                }
            )
        row = (
            conn.execute(
                select(documents).where(
                    *where(
                        service.tenant,
                        record["owner"],
                        "trash_action",
                        payload["operation_id"],
                    )
                )
            )
            .mappings()
            .one()
        )
        value = copy.deepcopy(row["payload"])
        project_versions = {
            project: conn.execute(
                select(documents.c.version).where(
                    documents.c.tenant_id == service.tenant,
                    documents.c.owner_id == record["owner"],
                    documents.c.project_id == "__projects",
                    documents.c.kind == "project",
                    documents.c.document_id == project,
                )
            ).scalar_one()
            for project in value["response"]["project_versions"]
        }
        value["response"].update(
            state="completed",
            items=final,
            project_versions=project_versions,
            removed=plan["counts"],
            kept=plan["kept"],
            media_deleted=bool(plan["objects"]),
        )
        conn.execute(
            update(documents)
            .where(
                *where(
                    service.tenant,
                    record["owner"],
                    "trash_action",
                    payload["operation_id"],
                )
            )
            .values(payload=value, updated_at=now)
        )
        payload.update(state="completed", completed_at=now)
        conn.execute(
            update(objects)
            .where(objects.c.id == record["id"])
            .values(payload=payload, updated_at=now)
        )
