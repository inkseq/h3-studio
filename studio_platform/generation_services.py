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
        if not isinstance(project_id, str) or not principal.allows(
            project_id, operation
        ):
            raise NotFound("project_not_found")
        return self.repo.get_document(
            self.project_scope(principal), "project", project_id
        )

    def plan(self, principal, plan_id):
        with self.repo.engine.connect() as conn:
            row = (
                conn.execute(
                    select(plans).where(
                        plans.c.id == plan_id,
                        plans.c.tenant_id == self.tenant_id,
                        plans.c.owner_id == principal.owner,
                    )
                )
                .mappings()
                .first()
            )
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
            row = (
                conn.execute(
                    select(artifacts)
                    .join(jobs, artifacts.c.job_id == jobs.c.id)
                    .where(
                        artifacts.c.id == artifact_id,
                        jobs.c.tenant_id == self.tenant_id,
                        jobs.c.owner_id == principal.owner,
                    )
                )
                .mappings()
                .first()
            )
        if not row:
            raise NotFound("artifact_not_found")
        self.job(principal, row["job_id"])
        if row["metadata"].get("content_deleted"):
            raise NotFound("artifact_not_found")
        from .content_lifecycle import assert_content_available

        with self.repo.engine.connect() as connection:
            from .artifact_renditions import physical_objects

            assert_content_available(
                connection,
                self.tenant_id,
                principal.owner,
                artifact_ids=(artifact_id,),
                object_keys=tuple(
                    value.get("key", value.get("object_key"))
                    for value in (row["metadata"], *physical_objects(row["metadata"]))
                ),
            )
        return row


class GenerationRead:
    def __init__(self, repo, access):
        self.repo, self.access = repo, access

    @staticmethod
    def project(record):
        return {
            "id": record["document_id"],
            "version": record["version"],
            "updated_at": record["updated_at"],
            "project": record["payload"],
        }

    @staticmethod
    def execution(execution):
        # User-relevant admission, never operator approval/budget identities.
        from .credits_pricing import public_quote

        value = {
            "admission_state": execution.get(
                "admission_state", "queued" if execution.get("enabled") else "blocked"
            ),
            "enabled": bool(execution.get("enabled")),
            "quote_known": bool(execution.get("quote_known")),
            "backend": execution.get("backend"),
            **public_quote(execution),
            **(
                {"budget_error_code": "budget_exceeded"}
                if execution.get("budget_error_code") == "budget_exceeded"
                else {}
            ),
            **(
                {"deployment_profile_id": execution["deployment_profile_id"]}
                if execution.get("deployment_profile_id")
                else {}
            ),
            **(
                {"timing_hint": execution["timing_hint"]}
                if execution.get("timing_hint")
                else {}
            ),
            **(
                {"delivery_spec": execution["delivery_spec"]}
                if "delivery_spec" in execution
                else {}
            ),
        }
        frozen = execution.get("credit_quote", {})
        if isinstance(frozen, dict) and frozen.get("version") == 2:
            value["budget_rows"] = [
                {key: row[key] for key in ("scope", "budget_role")}
                for row in frozen["budget_rows"]
            ]
        return value

    @staticmethod
    def artifact(record):
        from .media_renditions import public_artifact_renditions

        value = record["metadata"]
        return {
            "id": record["id"],
            "job_id": record["job_id"],
            "kind": value["kind"],
            "mime": value.get(
                "mime", value.get("content_type", "application/octet-stream")
            ),
            "size_bytes": value["size_bytes"],
            "sha256": value["sha256"],
            "metadata": {
                k: v
                for k, v in value.items()
                if k
                not in {
                    "object_key",
                    "provider",
                    "storage_profile",
                    "display_renditions",
                }
            },
            "content_url": f'/v1/artifacts/{record["id"]}/content',
            "download_url": f'/v1/artifacts/{record["id"]}/content?download=1',
            **public_artifact_renditions(record),
        }

    def public_job(
        self, job, *, artifact_records=None, cancellation=None, principal=None
    ):
        from .credits_pricing import public_quote, hide_dollar_estimate, user_result

        stored_request = job["request"]
        visible = {
            k: job.get(k)
            for k in (
                "id",
                "status",
                "created_at",
                "updated_at",
                "request_hash",
                "error_code",
                "result",
                "created",
            )
        }
        visible['result'] = user_result(
            visible.get('result'),
            hide_dollars=hide_dollar_estimate(job['execution_plan'], principal),
        )
        from .capacity_reclaim import history

        visible["capacity_reclaim_count"] = history(job)[0]
        if isinstance(visible["result"], dict):
            # Internal fencing evidence and engine heartbeat are not client
            # authoring fields. The bounded count is the public contract.
            visible["result"] = {
                key: value
                for key, value in visible["result"].items()
                if key not in {"capacity_reclaims", "capacity_progress"}
            }
        visible.update(
            **public_quote(job["execution_plan"]),
            client_ref=stored_request.get("client_ref", {}),
            phase=job["status"],
            recipe_id=stored_request.get("recipe_id"),
            effective_request=stored_request.get("request", {}),
            simulation=job["execution_plan"].get("backend") == "mock"
            or stored_request.get("simulation") is True,
            plan_id=job["plan_id"],
            project_id=job["project_id"],
            artifacts=[],
        )
        if cancellation is None:
            cancellation = self.repo.get_job_cancellation_fields(
                self.access.tenant_id,
                job["owner_id"],
                {job["id"]: job["project_id"]},
                expected_states={
                    job["id"]: {
                        key: job[key] for key in ("status", "updated_at", "error_code")
                    }
                },
            )[job["id"]]
        visible.update(cancellation)
        if job.get("error_code") is not None:
            from .generation_diagnostics import safe_failure_summary

            result = job.get("result") if isinstance(job.get("result"), dict) else {}
            diagnostic = result.get("failure_diagnostic")
            visible["failure_diagnostic"] = (
                diagnostic
                if isinstance(diagnostic, dict)
                else safe_failure_summary(job)
            )
            if isinstance(diagnostic, dict) and diagnostic.get("notice_id"):
                # Clients coalesce by this stable identity. The event feed
                # carries the authoritative first-committed new_kind flag.
                visible["failure_notice"] = {"id": diagnostic["notice_id"]}
        if stored_request.get("content_deleted"):
            visible["content_deleted"] = True
            visible["retry_advice"] = {
                **visible["retry_advice"],
                "safe_to_retry": False,
            }
        if stored_request.get("deployment_profile_id"):
            visible["deployment_profile_id"] = stored_request["deployment_profile_id"]
        if "delivery_spec" in job["execution_plan"]:
            visible["delivery_spec"] = job["execution_plan"]["delivery_spec"]
        if job["status"] == "succeeded":
            if artifact_records is None:
                artifact_records = self.repo.list_artifacts(
                    Scope(self.access.tenant_id, job["owner_id"], job["project_id"]),
                    job["id"],
                )
            for art in artifact_records:
                if not art["metadata"].get("content_deleted"):
                    visible["artifacts"].append(self.artifact(art))
        return visible

    def job(self, principal, job_id):
        job = self.access.job(principal, job_id)
        value = self.public_job(job, principal=principal)
        if job["status"] in {"queued", "waiting_capacity"}:
            from .control import WorkerControl
            from .dstack_demand import job_wait_stage

            value["wait_diagnostic"] = WorkerControl(self.repo).queued_diagnostic(job)
            with self.repo.engine.connect() as connection:
                value["wait_diagnostic"]["stage"] = job_wait_stage(
                    connection, job, self.repo.clock()
                )
        return value

    def list_jobs(self, principal, *, project_id=None, limit=100, offset=0):
        if project_id:
            self.access.project(principal, project_id, "jobs:read")
        allowed = (
            (
                (None if principal.all_projects else principal.project_ids)
                if "jobs:read" in principal.scopes
                else ()
            )
            if principal.machine
            else None
        )
        values = self.repo.list_jobs_for_owner(
            self.access.tenant_id,
            principal.owner,
            project_id=project_id,
            project_ids=allowed,
            limit=limit,
            offset=offset,
            summary=True,
        )
        visible = [v for v in values if principal.allows(v["project_id"], "jobs:read")]
        cancellation = self.repo.get_job_cancellation_fields(
            self.access.tenant_id,
            principal.owner,
            {v["id"]: v["project_id"] for v in visible},
            expected_states={
                v["id"]: {key: v[key] for key in ("status", "updated_at", "error_code")}
                for v in visible
            },
        )
        media = self.repo.list_artifacts_for_jobs(
            self.access.tenant_id,
            principal.owner,
            {v["id"]: v["project_id"] for v in visible if v["status"] == "succeeded"},
        )
        return {
            "jobs": [
                self.public_job(
                    v,
                    artifact_records=media.get(v["id"], []),
                    cancellation=cancellation[v["id"]],
                    principal=principal,
                )
                for v in visible
            ]
        }


def freeze_generation_policy(repo, tenant, owner, compiled, execution):
    """Bind policy after each creator's final server-side request changes."""
    from .content_policy import check_input, require_generation_terms

    moderation = check_input(repo, tenant, owner, compiled)
    if moderation is None:
        compiled.pop("content_moderation", None)
    else:
        compiled["content_moderation"] = moderation
    with repo.engine.connect() as connection:
        terms = require_generation_terms(connection, tenant, owner, execution, compiled)
    if terms is None:
        execution.pop("terms_acceptance", None)
    else:
        execution["terms_acceptance"] = terms


class GenerationPlanning:
    def __init__(self, *, repo, assets, settings, policies, access, read):
        self.repo, self.assets, self.settings, self.policies = (
            repo,
            assets,
            settings,
            policies,
        )
        self.access, self.read = access, read

    def create(
        self,
        principal,
        body,
        project,
        *,
        plan_id=None,
        authoring_recipe=None,
        authoring_context=None,
        defaults_version=None,
        stored_selection=False,
    ):
        # Entry-point authorization is performed by GenerationAccess before
        # this call; preflight also checks the editable source around derivation.
        project_id = project["id"]
        from .quality_authoring import normalize

        body, quality = normalize(
            self.settings, self.repo, principal, body, stored=stored_selection
        )
        compiled, fingerprint = compile_request(
            body,
            lambda asset_id: self.assets.model_snapshot(
                principal.owner, project_id, asset_id
            ),
            backend=self.settings.execution_backend,
        )
        if quality is not None:
            from .repository import request_hash

            compiled["quality_selection"] = quality
            fingerprint = request_hash(compiled)
        ref = compiled["client_ref"]
        if not validate_source_ref(project, ref):
            raise Conflict("shot_version_conflict")
        compiled["server_source_hash"] = source_snapshot(project, ref["shot_id"])
        if authoring_recipe is not None:
            import copy

            compiled["authoring_recipe"] = copy.deepcopy(authoring_recipe)
            compiled["authoring_recipe"]["controls"]["seed"] = str(
                compiled["request"]["seed"]
            )
            compiled["authoring_context"] = copy.deepcopy(authoring_context)
            compiled["authoring_defaults_version"] = defaults_version
        admission = self.policies.evaluate(
            compiled, self.access.scope(principal, project_id), fingerprint
        )
        execution = admission.execution
        if compiled.get("deployment_profile_id"):
            from .runtime_catalog import timing_hint

            execution["deployment_profile_id"] = compiled["deployment_profile_id"]
            inputs, output = compiled["request"]["inputs"], compiled["output_spec"]
            roles = [
                role
                for key, role in (
                    ("images", "image"),
                    ("videos", "video"),
                    ("audios", "audio"),
                    ("first_frame", "first_frame"),
                    ("last_frame", "last_frame"),
                )
                if inputs.get(key)
            ]
            hint = timing_hint(
                compiled["deployment_profile_id"],
                compiled["request"]["mode"],
                output["width"],
                output["height"],
                output["frames"],
                24,
                compiled["request"]["steps"],
                roles,
            )
            if hint:
                execution["timing_hint"] = hint
        enabled, blockers = execution["enabled"], execution["blockers"]
        simulation = self.settings.execution_backend == "mock"
        from .credits_pricing import (
            hide_dollar_estimate,
            public_quote,
            quote,
            user_estimate,
        )

        with self.repo.engine.connect() as connection:
            credit_quote = quote(
                connection,
                self.access.tenant_id,
                principal.owner,
                compiled,
                execution=execution,
            )
            if credit_quote is not None:
                execution["credit_quote"] = credit_quote
        freeze_generation_policy(
            self.repo, self.access.tenant_id, principal.owner, compiled, execution
        )
        hide_dollars = hide_dollar_estimate(execution, principal)
        plan = self.repo.create_plan(
            self.access.scope(principal, project_id),
            compiled,
            execution,
            expires_at=admission.expires_at,
            estimated_cost_microusd=admission.cost,
            plan_id=plan_id,
        )
        if plan_id is not None:
            return self.retained_plan(plan, principal)
        return {
            "plan_id": plan["id"],
            "status": "ready" if enabled else "blocked",
            "request_hash": plan["request_hash"],
            "effective_request": compiled["request"],
            "output_spec": compiled["output_spec"],
            "client_ref": ref,
            "expires_at": plan["expires_at"],
            "blockers": blockers,
            "warnings": ["本地模拟：不调用模型，不代表H3速度或质量"]
            if simulation
            else [],
            "estimate": user_estimate(admission.estimate, hide_dollars=hide_dollars),
            **public_quote(execution),
            "execution": self.read.execution(execution),
            "simulation": simulation,
        }

    def retained_plan(self, plan, principal=None):
        compiled, execution = plan["request"], plan["execution_plan"]
        from .credits_pricing import hide_dollar_estimate, public_quote, user_estimate

        hide_dollars = hide_dollar_estimate(execution, principal)
        return {
            "plan_id": plan["id"],
            "status": "ready" if execution.get("enabled") else "blocked",
            "request_hash": plan["request_hash"],
            "effective_request": compiled["request"],
            "output_spec": compiled["output_spec"],
            "client_ref": compiled["client_ref"],
            "expires_at": plan["expires_at"],
            "blockers": execution.get("blockers", []),
            "estimate": user_estimate(
                {
                    "cost_microusd": plan["estimated_cost_microusd"]
                    if execution.get("quote_known") is True
                    else None,
                    "final_bill": False,
                },
                hide_dollars=hide_dollars,
            ),
            **public_quote(execution),
            "execution": self.read.execution(execution),
            "simulation": execution.get("backend") == "mock",
        }

    def check_source(self, project, compiled, principal=None):
        if "film_source_ref" in compiled:
            if principal is None:
                raise Conflict("film_source_not_authorized")
            from .film_runs import validate_film_source

            scope = self.access.scope(principal, project["id"])
            with self.repo.engine.connect() as connection:
                validate_film_source(connection, self.repo, scope, compiled)
            return
        if "authoring_defaults_version" in compiled:
            from .storyboard_versions import preferences

            if (
                principal is None
                or preferences(self.repo, self.access.tenant_id, principal.owner)[
                    "version"
                ]
                != compiled["authoring_defaults_version"]
            ):
                raise Conflict("storyboard_defaults_changed")
        if compiled["recipe_id"] == RENDER_RECIPE:
            if not validate_render_source(project, compiled):
                raise Conflict("chapter_timeline_changed")
        else:
            ref = compiled["client_ref"]
            if not validate_source_ref(project, ref) or compiled.get(
                "server_source_hash"
            ) != source_snapshot(project, ref["shot_id"]):
                raise Conflict("shot_version_conflict")
