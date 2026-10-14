"""HTTP-independent access, planning and read projections for the existing ledger.

These services use the authenticated Principal; they do not authenticate HTTP,
own a second queue, change policy, provision capacity or run a model. Callers
must authorize access before using the raw public projection helpers.
"""
from sqlalchemy import select

from .capabilities import compile_request
from .render_plans import RECIPE as RENDER_RECIPE, validate_render_source
from .repository import Scope, NotFound, Conflict, plans, artifacts, jobs
from .source_snapshot import source_snapshot, validate_source_ref


class GenerationAccess:
    def __init__(self, repo, tenant_id):
        self.repo, self.tenant_id = repo, tenant_id

    def scope(self, principal, project_id):
        return Scope(self.tenant_id, principal.owner, project_id, principal.actor_id)

    def project_scope(self, principal):
        return self.scope(principal, "__projects")

    def project(self, principal, project_id, operation="projects:read"):
        if not isinstance(project_id, str) or not principal.allows(project_id, operation):
            raise NotFound("project_not_found")
        return self.repo.get_document(self.project_scope(principal), "project", project_id)

    def plan(self, principal, plan_id):
        with self.repo.engine.connect() as conn:
            row = conn.execute(select(plans).where(plans.c.id == plan_id,
                plans.c.tenant_id == self.tenant_id, plans.c.owner_id == principal.owner)).mappings().first()
        if not row:
            raise NotFound("plan_not_found")
        self.project(principal, row["project_id"], "jobs:write")
        return dict(row)

    def job(self, principal, job_id, operation="jobs:read"):
        job = self.repo.get_job_for_owner(self.tenant_id, principal.owner, job_id)
        self.project(principal, job["project_id"], operation)
        return job

    def artifact(self, principal, artifact_id):
        with self.repo.engine.connect() as conn:
            row = conn.execute(select(artifacts).join(jobs, artifacts.c.job_id == jobs.c.id).where(
                artifacts.c.id == artifact_id, jobs.c.tenant_id == self.tenant_id,
                jobs.c.owner_id == principal.owner)).mappings().first()
        if not row:
            raise NotFound("artifact_not_found")
        self.job(principal, row["job_id"])
        return row


class GenerationRead:
    def __init__(self, repo, access):
        self.repo, self.access = repo, access

    @staticmethod
    def project(record):
        return {"id": record["document_id"], "version": record["version"],
                "updated_at": record["updated_at"], "project": record["payload"]}

    @staticmethod
    def execution(execution):
        # User-relevant admission, never operator approval/budget identities.
        return {"admission_state": execution.get("admission_state", "queued" if execution.get("enabled") else "blocked"),
                "enabled": bool(execution.get("enabled")), "quote_known": bool(execution.get("quote_known")),
                "backend": execution.get("backend"),
                **({"budget_error_code": "budget_exceeded"} if execution.get("budget_error_code") == "budget_exceeded" else {}),
                **({"deployment_profile_id": execution["deployment_profile_id"]} if execution.get("deployment_profile_id") else {}),
                **({"timing_hint": execution["timing_hint"]} if execution.get("timing_hint") else {}),
                **({"delivery_spec": execution["delivery_spec"]} if "delivery_spec" in execution else {})}

    @staticmethod
    def artifact(record):
        value = record["metadata"]
        return {"id": record["id"], "job_id": record["job_id"], "kind": value["kind"],
                "mime": value.get("mime", value.get("content_type", "application/octet-stream")),
                "size_bytes": value["size_bytes"], "sha256": value["sha256"],
                "metadata": {k: v for k, v in value.items() if k not in {"object_key", "provider", "storage_profile"}},
                "content_url": f'/v1/artifacts/{record["id"]}/content',
                "download_url": f'/v1/artifacts/{record["id"]}/content?download=1'}

    def public_job(self, job, *, artifact_records=None):
        stored_request = job["request"]
        visible = {k: job.get(k) for k in ("id", "status", "created_at", "updated_at", "request_hash", "error_code", "result", "created")}
        visible.update(client_ref=stored_request.get("client_ref", {}), phase=job["status"],
            recipe_id=stored_request.get("recipe_id"), effective_request=stored_request.get("request", {}),
            simulation=job["execution_plan"].get("backend") == "mock" or stored_request.get("simulation") is True, plan_id=job["plan_id"],
            project_id=job["project_id"], artifacts=[])
        if stored_request.get("deployment_profile_id"):
            visible["deployment_profile_id"] = stored_request["deployment_profile_id"]
        if "delivery_spec" in job["execution_plan"]:
            visible["delivery_spec"] = job["execution_plan"]["delivery_spec"]
        if job["status"] == "succeeded":
            if artifact_records is None:
                artifact_records = self.repo.list_artifacts(Scope(self.access.tenant_id, job["owner_id"], job["project_id"]), job["id"])
            for art in artifact_records:
                visible["artifacts"].append(self.artifact(art))
        return visible

    def job(self, principal, job_id):
        job = self.access.job(principal, job_id)
        value = self.public_job(job)
        if job["status"] == "queued":
            from .control import WorkerControl
            value["wait_diagnostic"] = WorkerControl(self.repo).queued_diagnostic(job)
        return value

    def list_jobs(self, principal, *, project_id=None, limit=100, offset=0):
        if project_id:
            self.access.project(principal, project_id, "jobs:read")
        allowed = ((None if principal.all_projects else principal.project_ids) if "jobs:read" in principal.scopes else ()) if principal.machine else None
        values = self.repo.list_jobs_for_owner(self.access.tenant_id, principal.owner, project_id=project_id, project_ids=allowed,
                                               limit=limit, offset=offset, summary=True)
        visible = [v for v in values if principal.allows(v["project_id"], "jobs:read")]
        media = self.repo.list_artifacts_for_jobs(self.access.tenant_id, principal.owner,
            {v["id"]: v["project_id"] for v in visible if v["status"] == "succeeded"})
        return {"jobs": [self.public_job(v, artifact_records=media.get(v["id"], [])) for v in visible]}


class GenerationPlanning:
    def __init__(self, *, repo, assets, settings, policies, access, read):
        self.repo, self.assets, self.settings, self.policies = repo, assets, settings, policies
        self.access, self.read = access, read

    def create(self, principal, body, project):
        # Entry-point authorization is performed by GenerationAccess before
        # this call; preflight also checks the editable source around derivation.
        project_id = project["id"]
        compiled, fingerprint = compile_request(body,
            lambda asset_id: self.assets.model_snapshot(principal.owner, project_id, asset_id),
            backend=self.settings.execution_backend)
        ref = compiled["client_ref"]
        if not validate_source_ref(project, ref):
            raise Conflict("shot_version_conflict")
        compiled["server_source_hash"] = source_snapshot(project, ref["shot_id"])
        admission = self.policies.evaluate(compiled, self.access.scope(principal, project_id), fingerprint)
        execution = admission.execution
        if compiled.get("deployment_profile_id"):
            from .runtime_catalog import timing_hint
            execution["deployment_profile_id"] = compiled["deployment_profile_id"]
            inputs, output = compiled["request"]["inputs"], compiled["output_spec"]
            roles = [role for key, role in (("images", "image"), ("videos", "video"), ("audios", "audio"),
                     ("first_frame", "first_frame"), ("last_frame", "last_frame")) if inputs.get(key)]
            hint = timing_hint(compiled["deployment_profile_id"], compiled["request"]["mode"],
                output["width"], output["height"], output["frames"], 24, compiled["request"]["steps"], roles)
            if hint:
                execution["timing_hint"] = hint
        enabled, blockers = execution["enabled"], execution["blockers"]
        simulation = self.settings.execution_backend == "mock"
        plan = self.repo.create_plan(self.access.scope(principal, project_id), compiled, execution,
            expires_at=admission.expires_at, estimated_cost_microusd=admission.cost)
        return {"plan_id": plan["id"], "status": "ready" if enabled else "blocked", "request_hash": plan["request_hash"],
                "effective_request": compiled["request"], "output_spec": compiled["output_spec"],
                "client_ref": ref, "expires_at": plan["expires_at"], "blockers": blockers,
                "warnings": ["本地模拟：不调用模型，不代表H3速度或质量"] if simulation else [],
                "estimate": admission.estimate, "execution": self.read.execution(execution), "simulation": simulation}

    @staticmethod
    def check_source(project, compiled):
        if compiled["recipe_id"] == RENDER_RECIPE:
            if not validate_render_source(project, compiled):
                raise Conflict("chapter_timeline_changed")
        else:
            ref = compiled["client_ref"]
            if (not validate_source_ref(project, ref)
                    or compiled.get("server_source_hash") != source_snapshot(project, ref["shot_id"])):
                raise Conflict("shot_version_conflict")
