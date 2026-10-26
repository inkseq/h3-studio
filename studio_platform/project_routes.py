"""Project CRUD HTTP endpoints over the existing generation/guided services."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Header, HTTPException, Query, Request
from sqlalchemy import select

from .generation_admission import reject_managed
from .project_validation import validate_project
from .quality_http import QualityRoute


def create_project_router(app) -> APIRouter:
    """Keep principal resolution, CAS and mutation ownership in existing services."""
    router = APIRouter(route_class=QualityRoute)
    repo, settings = app.state.repository, app.state.settings
    access, read = app.state.generation_access, app.state.generation_read
    project_scope, authorized_project = access.project_scope, access.project
    project_response = read.project

    @router.get("/v1/projects")
    def list_projects(
        request: Request,
        limit: int = Query(100, ge=1, le=100),
        offset: int = Query(0, ge=0, le=1000000),
    ):
        principal = request.state.principal
        allowed = (
            (
                (None if principal.all_projects else principal.project_ids)
                if "projects:read" in principal.scopes
                else ()
            )
            if principal.machine
            else None
        )
        values = repo.list_documents(
            project_scope(principal),
            "project",
            limit=limit,
            offset=offset,
            allowed_ids=allowed,
            summary=True,
            exclude_managed=True,
        )
        return {
            "projects": [
                {
                    "id": v["document_id"],
                    "title": v["payload"]["title"],
                    "version": v["version"],
                    "updated_at": v["updated_at"],
                }
                for v in values
                if principal.allows(v["document_id"], "projects:read")
            ]
        }

    @router.post("/v1/projects", status_code=201)
    def create_project(
        request: Request, body: dict, idempotency_key: str | None = Header(None)
    ):
        principal = request.state.principal
        if principal.machine and not (
            principal.all_projects and "projects:create" in principal.scopes
        ):
            raise HTTPException(403, "创建故事需要projects:create及全部本人项目授权")
        from .guided import empty_project

        if "project" in body:
            if set(body) != {"project"}:
                raise HTTPException(422, "导入故事仅接受project字段")
            project = body["project"]
        else:
            values = dict(body)
            if idempotency_key and "id" not in values:
                values["id"] = (
                    "project-"
                    + uuid.uuid5(
                        uuid.NAMESPACE_URL,
                        f"{settings.tenant_id}:{principal.owner}:{principal.actor_id}:{idempotency_key}",
                    ).hex
                )
            project = empty_project(values)
        validate_project(project, settings.max_project_bytes)
        reject_managed(project)
        return app.state.guided.mutate(
            principal, project["id"], project, idempotency_key, create=True
        )

    @router.get("/v1/projects/{project_id}")
    def get_project(project_id: str, request: Request):
        from .universal_trash import active_target

        with repo.engine.connect() as connection:
            if active_target(
                connection,
                settings.tenant_id,
                request.state.principal.owner,
                {"kind": "project", "project_id": project_id, "id": project_id},
            ):
                raise HTTPException(404, "project_in_trash")
        record = authorized_project(request.state.principal, project_id)
        if record["payload"].get("content_deleted"):
            raise HTTPException(404, "project_not_found")
        from .agent_management import ATTRIBUTION_PROJECT
        from .repository import documents

        with repo.engine.connect() as conn:
            rows = conn.execute(
                select(documents.c.payload).where(
                    documents.c.tenant_id == settings.tenant_id,
                    documents.c.owner_id == request.state.principal.owner,
                    documents.c.project_id == ATTRIBUTION_PROJECT,
                    documents.c.kind == 'created_by',
                    documents.c.payload['project_id'].as_string() == project_id,
                )
            ).scalars()
            attribution = {
                r['resource_id']: {k: r[k] for k in ('kind', 'connection_id', 'name')}
                for r in rows
            }
        return {**project_response(record), 'created_by': attribution}

    @router.put("/v1/projects/{project_id}")
    def save_project(project_id: str, request: Request, body: dict):
        principal = request.state.principal
        if principal.machine and not principal.allows(project_id, "projects:write"):
            raise HTTPException(403, "保存故事需要projects:write授权")
        reject_managed(
            authorized_project(principal, project_id, "projects:write")["payload"]
        )
        project = validate_project(body.get("project"), settings.max_project_bytes)
        reject_managed(project)
        if project["id"] != project_id or type(body.get("expected_version")) is not int:
            raise HTTPException(422, "需要匹配的项目ID与版本")
        return app.state.guided.save(
            principal, project_id, project, body["expected_version"]
        )

    return router
