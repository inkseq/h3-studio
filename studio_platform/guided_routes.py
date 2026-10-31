"""Guided authoring HTTP boundary; mutations remain in the Guided service."""

from __future__ import annotations

import csv
import io
import json
import math
import re

from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.responses import Response
from sqlalchemy import select

from .auth import API_SCOPES, AuthenticationError, LoginLimited
from .caption_server import caption_signature
from .guided import checked_captions, object_fields
from .guided_schema import ACTION_FIELDS, contract as guided_contract
from .project_validation import TYPES, ROLES
from .quality_http import QualityRoute
from .render_plans import ordered_chapter
from .repository import Conflict, NotFound, documents, request_hash


def create_guided_router(app, service) -> APIRouter:
    router = APIRouter(route_class=QualityRoute)
    auth = app.state.auth

    def browser(request):
        p = request.state.principal
        if p.machine:
            raise HTTPException(403, "API key管理仅允许网页登录会话")
        return p

    @router.get("/v1/api-keys")
    def list_keys(request: Request):
        return {
            "api_keys": auth.list_keys(browser(request).owner),
            "available_scopes": sorted(API_SCOPES),
        }

    @router.post("/v1/auth/password")
    def change_password(request: Request, body: dict):
        p = browser(request)
        object_fields(
            body,
            {"old_password", "new_password"},
            "修改密码仅接受old_password与new_password",
        )
        try:
            auth.change_password(
                p.owner, body.get("old_password"), body.get("new_password")
            )
        except LoginLimited as error:
            raise HTTPException(
                429, str(error), headers={"Retry-After": str(error.retry_after)}
            ) from None
        except AuthenticationError as error:
            raise HTTPException(401, str(error)) from None
        from fastapi.responses import JSONResponse

        response = JSONResponse({"changed": True, "reauthenticate": True})
        response.delete_cookie("sixnine_session", path="/")
        return response

    @router.post("/v1/api-keys", status_code=201)
    def create_key(request: Request, body: dict):
        p = browser(request)
        object_fields(
            body,
            {"name", "scopes", "project_ids", "all_projects", "expires_in_days"},
            "API key配置字段无效",
        )
        ids = body.get("project_ids", [])
        if not isinstance(ids, list):
            raise ValueError("project_ids必须为数组")
        for project_id in ids:
            app.state.authorized_project(p, project_id)
        try:
            result = auth.create_key(
                p.owner,
                authenticated_session=p,
                name=body.get("name"),
                scopes=body.get("scopes"),
                project_ids=ids,
                all_projects=body.get("all_projects", False),
                expires_in_days=body.get("expires_in_days", 90),
            )
            token = result.pop("api_key")
            return {"api_key": token, "key": result}
        except AuthenticationError as error:
            raise HTTPException(401, str(error)) from None

    @router.delete("/v1/api-keys/{key_id}")
    def revoke_key(key_id: str, request: Request):
        value = auth.revoke_key(browser(request).owner, key_id)
        if value is None:
            raise NotFound("api_key_not_found")
        return value

    @router.get("/v1/guided-schema")
    def schema():
        from .take_reviews import contract as take_reviews_contract
        from .take_redo import contract as take_redo_contract
        from .product_feedback import contract as feedback_contract
        from .inky_routes import contract as inky_contract

        return {
            "take_reviews": take_reviews_contract(),
            "take_redo": take_redo_contract(),
            "feedback": feedback_contract(),
            "inky": inky_contract(),
            "version": 1,
            "entity_types": sorted(TYPES),
            "link_roles": sorted(ROLES),
            "scopes": sorted(API_SCOPES),
            "max_actions": 200,
            "max_entities": 5000,
            "actions": list(ACTION_FIELDS),
            "merge_semantics": "entity.data and journey patch merge one level; nested values replace",
            "example": {
                "expected_version": 1,
                "actions": [
                    {
                        "op": "entity.create",
                        "entity": {
                            "id": "chapter-one",
                            "type": "chapter",
                            "title": "第一章",
                        },
                    }
                ],
            },
            **guided_contract(),
        }

    @router.get("/v1/projects/{project_id}/meta")
    def project_meta(project_id: str, request: Request):
        p = request.state.principal
        if not p.allows(project_id, "projects:read"):
            raise NotFound("project_not_found")
        with service.repo.engine.connect() as conn:
            row = (
                conn.execute(
                    select(
                        documents.c.version,
                        documents.c.updated_at,
                        documents.c.payload["title"].as_string().label("title"),
                    ).where(
                        documents.c.tenant_id == service.settings.tenant_id,
                        documents.c.owner_id == p.owner,
                        documents.c.project_id == "__projects",
                        documents.c.kind == "project",
                        documents.c.document_id == project_id,
                    )
                )
                .mappings()
                .first()
            )
        if row is None:
            raise NotFound("project_not_found")
        return {"id": project_id, **dict(row)}

    @router.get("/v1/agent-guide")
    def guide():
        from .agent_discovery import public_guide

        return {
            "version": 2,
            "schema_url": "/v1/guided-schema",
            "openapi_url": "/openapi.json",
            "skill_download_url": "/v1/agent-skill.zip",
            "quick_creation": public_guide()["quick_creation"],
            "examples_url": "/for-agents/guide.json",
            "generation_availability": public_guide()["generation_availability"],
            "story_risk_review": public_guide()["story_risk_review"],
            "take_reviews": public_guide()["take_reviews"],
            "take_redo": public_guide()["take_redo"],
            "feedback": public_guide()["feedback"],
            "inky": public_guide()["inky"],
            "steps": [
                "Browser account creates scoped API key; pass Bearer on API calls only",
                "Before choosing quality and FL/REF, preflight and a new GPU submission, GET /v1/generation-availability; follow generation_availability for neutral tier/mode aliases, freshness and administrator/wait guidance. Read current capabilities for supported controls; unavailable tiers never fall back. Existing uncertain jobs retain their original receipt/key.",
                "For one clip, create workspace=freestyle; use project.journey.reviewShotId, shot.configure_generation and the saved-draft generation-plans route. Exact examples: /for-agents/guide.json",
                "POST /v1/projects with title/logline and stable Idempotency-Key; select returned project id",
                "POST /v1/projects/{id}/actions with expected_version and atomic actions",
                "POST /v1/assets multipart; asset.attach adds ready receipt to the same web document",
                "GET /v1/capabilities; POST /v1/generation-plans or /v1/render-plans; inspect blockers and estimate",
                "POST /v1/jobs with plan_id and stable Idempotency-Key; poll same job",
                "artifact.adopt attaches results; shot.select explicitly adopts with default clipSound=on and a validated same-job FLAC; sound.generated explicitly confirms/rebinds matching FLAC",
                "Use shot.segments in the same expected_version /actions CAS to keep ordered source-second ranges. The server binds them to the selected owned video and trusted duration; [] explicitly clears before selecting a different take. This edits the story only and never generates media.",
                "Browser checks project version and explicitly loads newer document without overwriting local drafts",
            ],
            "exports": [
                "/v1/projects/{id}/export?format=json",
                "/v1/projects/{id}/export?format=csv",
                "/v1/projects/{id}/chapters/{chapter_id}/subtitles.srt",
            ],
            "not_implemented": [
                "Automatic LLM writing",
                "Unconfigured image/music/Marble generation",
                "Shared team membership",
                "Server-side media ZIP",
            ],
        }

    @router.get("/v1/agent-skill.zip")
    def agent_skill(request: Request):
        principal = request.state.principal
        if principal.machine and "projects:read" not in principal.scopes:
            raise HTTPException(403, "下载Agent Skill需要projects:read权限")
        from .agent_discovery import skill_bundle

        return Response(
            skill_bundle(),
            media_type="application/zip",
            headers={
                "Content-Disposition": 'attachment; filename="inkseq-agent-skill.zip"'
            },
        )

    @router.get("/v1/projects/{project_id}/export")
    def export_project(project_id: str, request: Request, format: str = "json"):
        project = app.state.authorized_project(request.state.principal, project_id)[
            "payload"
        ]
        if format == "json":
            content, mime = (
                json.dumps(project, ensure_ascii=False, indent=2),
                "application/json",
            )
        elif format == "csv":
            out = io.StringIO(newline="")
            writer = csv.writer(out)
            writer.writerow(
                [
                    "chapter_id",
                    "chapter",
                    "scene_id",
                    "scene",
                    "shot_id",
                    "shot",
                    "seconds",
                    "prompt",
                    "selected_asset_id",
                ]
            )

            def cell(value):
                text = str(value)
                return (
                    "'" + text
                    if text.lstrip().startswith(("=", "+", "-", "@"))
                    else text
                )

            for chapter in sorted(
                (e for e in project["entities"] if e["type"] == "chapter"),
                key=lambda e: e["order"],
            ):
                _, scenes, shots = ordered_chapter(project, chapter["id"])
                for shot in shots:
                    scene = next(e for e in scenes if e["id"] == shot["parentId"])
                    writer.writerow(
                        [
                            cell(v)
                            for v in [
                                chapter["id"],
                                chapter["title"],
                                scene["id"],
                                scene["title"],
                                shot["id"],
                                shot["title"],
                                shot["data"].get("seconds", ""),
                                shot["data"].get("prompt", ""),
                                shot["data"].get("selectedAssetId", ""),
                            ]
                        ]
                    )
            content, mime = "\ufeff" + out.getvalue(), "text/csv"
        else:
            raise ValueError("format仅支持json或csv")
        return Response(
            content,
            media_type=mime,
            headers={"Content-Disposition": f'attachment; filename="story.{format}"'},
        )

    @router.get("/v1/projects/{project_id}/chapters/{chapter_id}/subtitles.srt")
    def subtitles(project_id: str, chapter_id: str, request: Request):
        project = app.state.authorized_project(request.state.principal, project_id)[
            "payload"
        ]
        shots, cues = checked_captions(project, chapter_id)
        track = project["journey"]["captionTracks"][chapter_id]
        if track.get("confirmedSnapshot") != caption_signature(
            project, chapter_id, shots
        ):
            raise Conflict("captions_need_review")

        def stamp(seconds):
            ms = math.floor(seconds * 1000 + 0.5)
            return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"

        parts = []
        for index, cue in enumerate(cues, 1):
            text = re.sub(
                r"\n\s*\n+",
                "\n",
                cue["text"].strip().replace("\r\n", "\n").replace("\r", "\n"),
            ).replace("\n", "\r\n")
            parts.append(
                f'{index}\r\n{stamp(cue["start"])} --> {stamp(cue["end"])}\r\n{text}\r\n'
            )
        return Response(
            "\r\n".join(parts),
            media_type="application/x-subrip",
            headers={"Content-Disposition": 'attachment; filename="subtitles.srt"'},
        )

    @router.get("/v1/projects/{project_id}/entities")
    def list_entities(
        project_id: str,
        request: Request,
        type: str | None = None,
        parent_id: str | None = None,
    ):
        row = app.state.authorized_project(request.state.principal, project_id)
        values = row["payload"]["entities"]
        return {
            "version": row["version"],
            "entities": [
                e
                for e in values
                if (type is None or e["type"] == type)
                and (
                    parent_id is None
                    or e["parentId"] == (None if parent_id == "root" else parent_id)
                )
            ],
        }

    @router.post("/v1/projects/{project_id}/actions")
    def actions(
        project_id: str,
        request: Request,
        body: dict,
        idempotency_key: str | None = Header(None),
    ):
        p = request.state.principal
        from .generation_admission import reject_managed

        reject_managed(
            app.state.authorized_project(p, project_id, "projects:write")["payload"]
        )
        try:
            return service.mutate(p, project_id, body, idempotency_key)
        except Conflict as error:
            if (
                str(error) == "document_version_conflict"
                and body.get("actions")
                and any(
                    isinstance(a, dict)
                    and str(a.get("op", "")).startswith(("storyboard.", "library."))
                    for a in body["actions"]
                )
                and all(
                    isinstance(a, dict)
                    and (
                        str(a.get("op", "")).startswith(("storyboard.", "library."))
                        or a.get("op") == "shot.configure_generation"
                    )
                    for a in body["actions"]
                )
            ):
                from fastapi.responses import JSONResponse

                return JSONResponse(
                    {
                        "detail": {
                            "code": "document_version_conflict",
                            "project_id": project_id,
                            "no_mutation_committed": True,
                            "request_hash": request_hash(body),
                            "message": "Story changed; reload before a new explicit edit. / 故事已更改，请刷新后重新编辑。",
                        }
                    },
                    status_code=409,
                )
            raise

    @router.post("/v1/projects/{project_id}/entities", status_code=201)
    def add_entity(
        project_id: str,
        request: Request,
        body: dict,
        idempotency_key: str | None = Header(None),
    ):
        object_fields(
            body, {"expected_version", "entity"}, "请求仅接受expected_version与entity"
        )
        return actions(
            project_id,
            request,
            {
                "expected_version": body.get("expected_version"),
                "actions": [{"op": "entity.create", "entity": body.get("entity")}],
            },
            idempotency_key,
        )

    @router.patch("/v1/projects/{project_id}/entities/{entity_id}")
    def patch_entity(
        project_id: str,
        entity_id: str,
        request: Request,
        body: dict,
        idempotency_key: str | None = Header(None),
    ):
        object_fields(
            body, {"expected_version", "patch"}, "请求仅接受expected_version与patch"
        )
        return actions(
            project_id,
            request,
            {
                "expected_version": body.get("expected_version"),
                "actions": [
                    {
                        "op": "entity.update",
                        "entity_id": entity_id,
                        "patch": body.get("patch"),
                    }
                ],
            },
            idempotency_key,
        )

    @router.delete("/v1/projects/{project_id}/entities/{entity_id}")
    def remove_entity(
        project_id: str,
        entity_id: str,
        request: Request,
        expected_version: int = Query(..., ge=1),
        cascade: bool = False,
        idempotency_key: str | None = Header(None),
    ):
        return actions(
            project_id,
            request,
            {
                "expected_version": expected_version,
                "actions": [
                    {"op": "entity.delete", "entity_id": entity_id, "cascade": cascade}
                ],
            },
            idempotency_key,
        )

    return router
