"""Generation plans, job views and artifact routes over shared services."""

from __future__ import annotations
import re
from urllib.parse import quote
from fastapi import Header, HTTPException, Query, Request
from .capabilities import VERSION, version_matches
from .media_renditions import RENDITIONS
from .repository import NotFound, Conflict
from .generation_admission import reject_managed
from .render_plans import compile_render


def register_routes(app, *, services, object_content, capabilities_provider):
    repo, settings, execution_policies = (
        services.repository,
        services.settings,
        services.policies,
    )
    generation_access, generation_read = services.access, services.read
    scope, authorized_project, owned_job = (
        generation_access.scope,
        generation_access.project,
        generation_access.job,
    )
    public_execution, public_job = generation_read.execution, generation_read.public_job

    @app.post("/v1/generation-plans", status_code=201)
    def make_plan(request: Request, body: dict):
        return generation_admission.plan(request.state.principal, body)

    @app.get("/v1/projects/{project_id}/shots/{shot_id}/generation-draft")
    def generation_draft(project_id: str, shot_id: str, request: Request):
        from .generation_draft import read_draft, shot_entity

        record = authorized_project(request.state.principal, project_id)
        project = record["payload"]
        from .storyboard_versions import preferences, effective_project

        defaults = preferences(repo, settings.tenant_id, request.state.principal.owner)
        rendered, provenance = effective_project(project, shot_id, defaults["defaults"])
        draft, issues = read_draft(
            rendered, shot_id, capabilities=capabilities_provider()
        )
        view = (
            "/freestyle"
            if project.get("journey", {}).get("workspace") == "freestyle"
            else "/"
        )
        return {
            "project_id": project_id,
            "shot_id": shot_id,
            "project_version": record["version"],
            "shot_version": shot_entity(project, shot_id)["version"],
            "draft": draft,
            "issues": issues,
            "defaults": provenance,
            "web_url": f"{view}?project={quote(project_id, safe='')}&entity={quote(shot_id, safe='')}",
        }

    @app.post(
        "/v1/projects/{project_id}/shots/{shot_id}/generation-plans", status_code=201
    )
    def saved_generation_plan(
        project_id: str, shot_id: str, request: Request, body: dict
    ):
        from .generation_draft import fields

        principal = request.state.principal
        fields(body, {"expected_version", "capabilities_version"}, "草稿预检")
        if (
            type(body.get("expected_version")) is not int
            or body["expected_version"] < 1
        ):
            raise ValueError("需要当前项目的expected_version")
        if not version_matches(body.get("capabilities_version", VERSION), VERSION):
            raise ValueError("生成能力已变化，请刷新后重新预检")
        authorized_project(principal, project_id, "projects:read")
        record = authorized_project(principal, project_id, "jobs:write")
        reject_managed(record["payload"])
        if record["version"] != body["expected_version"]:
            raise Conflict("document_version_conflict")
        return generation_admission.preflight(
            principal,
            record["payload"],
            shot_id,
            expected_version=body["expected_version"],
        )

    resolve_render_source = services.render_sources.resolve
    app.state.resolve_render_source = resolve_render_source

    @app.post("/v1/render-plans", status_code=201)
    def make_render_plan(request: Request, body: dict):
        principal = request.state.principal
        project_id = (
            body.get("client_ref", {}).get("project_id")
            if isinstance(body.get("client_ref"), dict)
            else None
        )
        project = authorized_project(principal, project_id, "jobs:write")["payload"]
        reject_managed(project)
        compiled, timeline, blockers, warnings = compile_render(
            body,
            project,
            lambda entity, kind: resolve_render_source(
                principal, project_id, entity, kind
            ),
        )
        compiled["render_blockers"] = blockers
        from .repository import request_hash

        fingerprint = request_hash(compiled)
        admission = execution_policies.evaluate(
            compiled, scope(principal, project_id), fingerprint
        )
        from .credits_pricing import (
            quote as quote_credits,
            public_quote,
            hide_dollar_estimate,
            user_estimate,
        )

        with repo.engine.connect() as connection:
            credit_quote = quote_credits(
                connection,
                settings.tenant_id,
                principal.owner,
                compiled,
                execution=admission.execution,
            )
        if credit_quote is not None:
            admission.execution["credit_quote"] = credit_quote
        from .generation_services import freeze_generation_policy

        freeze_generation_policy(
            repo, settings.tenant_id, principal.owner, compiled, admission.execution
        )
        plan = repo.create_plan(
            scope(principal, project_id),
            compiled,
            admission.execution,
            expires_at=admission.expires_at,
            estimated_cost_microusd=0,
        )
        return {
            "plan_id": plan["id"],
            "status": "ready" if admission.execution["enabled"] else "blocked",
            "request_hash": plan["request_hash"],
            "client_ref": compiled["client_ref"],
            "output_spec": compiled["output_spec"],
            "timeline": timeline,
            **public_quote(admission.execution),
            "expires_at": plan["expires_at"],
            "blockers": admission.execution["blockers"],
            "warnings": warnings,
            "estimate": user_estimate(
                admission.estimate,
                hide_dollars=hide_dollar_estimate(admission.execution, principal),
            ),
            "execution": public_execution(admission.execution),
            "simulation": compiled["simulation"],
        }

    generation_admission = services.admission
    app.state.generation_admission = generation_admission

    def create_from_plan(principal, plan_id, idempotency_key, initial_status=None):
        return generation_admission.create(
            principal, plan_id, idempotency_key, initial_status=initial_status
        )

    def enqueue_planned(
        principal, job, *, confirmation_required=False, spending_settings_version=None
    ):
        return generation_admission.enqueue(
            principal,
            job,
            confirmation_required=confirmation_required,
            spending_settings_version=spending_settings_version,
        )

    @app.post("/v1/jobs", status_code=202)
    def create_job(
        request: Request,
        body: dict,
        idempotency_key: str = Header(..., alias="Idempotency-Key"),
    ):
        if set(body) != {"plan_id"} or not isinstance(body["plan_id"], str):
            raise HTTPException(422, "只接受已确认的plan_id")
        return public_job(
            create_from_plan(request.state.principal, body["plan_id"], idempotency_key)
        )

    @app.get("/v1/jobs")
    def list_jobs(
        request: Request,
        client_project_id: str | None = None,
        limit: int = Query(100, ge=1, le=100),
        offset: int = Query(0, ge=0, le=1000000),
    ):
        return generation_read.list_jobs(
            request.state.principal,
            project_id=client_project_id,
            limit=limit,
            offset=offset,
        )

    from .job_events import register_routes as register_job_events

    register_job_events(app)

    @app.get("/v1/jobs/{job_id}")
    def get_job(job_id: str, request: Request):
        return generation_read.job(request.state.principal, job_id)

    @app.get("/v1/activity-summary")
    def activity_summary(request: Request, client_project_id: str):
        principal = request.state.principal
        authorized_project(principal, client_project_id, "jobs:read")
        from .diagnostics import job_activity

        return job_activity(
            repo,
            tenant_id=settings.tenant_id,
            owner_id=principal.owner,
            project_id=client_project_id,
        )

    @app.post("/v1/jobs/{job_id}/cancel")
    def cancel(job_id: str, request: Request):
        principal = request.state.principal
        job = owned_job(principal, job_id, "jobs:write")
        return public_job(
            repo.request_cancel(scope(principal, job["project_id"]), job_id)
        )

    @app.get("/v1/jobs/{job_id}/artifacts")
    def job_artifacts(job_id: str, request: Request):
        job = owned_job(request.state.principal, job_id)
        return {"artifacts": public_job(job)["artifacts"]}

    register_artifact_routes(app, services=services, object_content=object_content)

    app.state.create_from_plan = create_from_plan
    app.state.enqueue_planned = enqueue_planned


def register_artifact_routes(app, *, services, object_content):
    """Original artifact access/read/derived-frame boundary."""
    asset_service, generation_access = services.assets, services.access
    authorized_project, owned_job = generation_access.project, generation_access.job

    @app.post(
        "/v1/projects/{project_id}/artifacts/{artifact_id}/frames", status_code=201
    )
    def artifact_frame(
        project_id: str,
        artifact_id: str,
        request: Request,
        body: dict,
        idempotency_key: str = Header(..., alias="Idempotency-Key"),
    ):
        principal = request.state.principal
        authorized_project(principal, project_id, "assets:write")
        row = generation_access.artifact(principal, artifact_id)
        job = owned_job(principal, row["job_id"])
        if job["project_id"] != project_id:
            raise NotFound("artifact_not_found")
        if job["status"] != "succeeded":
            raise Conflict("artifact_not_ready")
        return asset_service.artifact_frame(
            principal.owner,
            project_id,
            principal.actor_id,
            artifact_id,
            job["id"],
            row["metadata"],
            body,
            idempotency_key,
        )

    @app.api_route("/v1/artifacts/{artifact_id}/content", methods=["GET", "HEAD"])
    def artifact_content(
        artifact_id: str,
        request: Request,
        download: bool = False,
        rendition: str | None = Query(None),
        expected_account: str | None = Query(
            None,
            description="Optional browser account fence for native media/navigation; does not grant access",
        ),
    ):
        principal = request.state.principal
        row = generation_access.artifact(principal, artifact_id)
        value = row["metadata"]
        if "rendition" in request.query_params:
            from .media_renditions import public_artifact_renditions

            if (
                request.query_params.getlist("rendition") != [rendition]
                or rendition not in RENDITIONS
            ):
                raise HTTPException(400, "invalid_rendition")
            advertised = (
                public_artifact_renditions(row).get("renditions", {}).get(rendition)
            )
            if advertised is None:
                raise HTTPException(404, "rendition_not_ready")
            item = value["display_renditions"]["objects"][rendition]
            return object_content(
                item["key"],
                "image/webp",
                f'{artifact_id}-{rendition}.webp',
                request,
                download=download,
                immutable_rendition=True,
            )
        extension = {"video": "mp4", "audio": "flac", "image": "png"}.get(
            value.get("kind"), "bin"
        )
        filename = value.get(
            "filename",
            f'inkseq-{row["job_id"][:8]}-{value.get("kind", "result")}.{extension}',
        )
        if isinstance(filename, str) and filename.lower().startswith("sixnine-"):
            filename = "inkseq-" + filename[len("sixnine-") :]
        mime = value.get("mime", value.get("content_type"))
        verified = (
            isinstance(value.get("sha256"), str)
            and re.fullmatch(r"[0-9a-f]{64}", value["sha256"]) is not None
            and type(value.get("size_bytes")) is int
            and value["size_bytes"] > 0
        )
        immutable_video = (
            verified and value.get("kind") == "video" and mime == "video/mp4"
        )
        return object_content(
            value["object_key"],
            value.get("mime", value.get("content_type", "application/octet-stream")),
            filename,
            request,
            download=download,
            immutable_video=immutable_video,
            immutable_media=not immutable_video
            and verified
            and isinstance(mime, str)
            and mime.split("/")[0] in {"image", "audio"},
        )
