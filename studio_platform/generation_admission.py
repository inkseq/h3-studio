"""Shared generation admission; HTTP and scenario callers retain their identity.

Extracted from the existing API and aligned with the preserved Quick Chat
admission boundary. This module does not own a queue, permissions or GPU calls.
"""

from .capabilities import capabilities
from .generation_draft import plan_body, read_draft
from .repository import Scope, Conflict, BudgetExceeded
from .render_plans import RECIPE as RENDER_RECIPE
from .source_snapshot import source_snapshot


def managed_project(project):
    return (
        project.get("integration_kind") == "quick_chat"
        or project.get("journey", {}).get("integration_kind") == "quick_chat"
    )


def reject_managed(project):
    if managed_project(project):
        raise Conflict("quick_chat_managed_resource")


class GenerationAdmission:
    def __init__(
        self,
        *,
        repo,
        assets,
        settings,
        policies,
        access,
        planning,
        capabilities_provider=None,
    ):
        self.repo, self.assets, self.settings, self.policies = (
            repo,
            assets,
            settings,
            policies,
        )
        self.access, self.planning = access, planning
        self.capabilities_provider = capabilities_provider or (
            lambda: capabilities(self.settings)
        )

    def task_scope(self, principal, project_id, business_identity=None):
        if business_identity is None:
            return self.access.scope(principal, project_id)
        # Only a queue idempotency namespace; the real caller is authorized
        # separately on every operation and recorded by the authoring service.
        return Scope(
            self.settings.tenant_id, principal.owner, project_id, "quick-chat-execution"
        )

    def plan(self, principal, body):
        """Legacy direct request: authorize the source before creating a plan."""
        project_id = (
            body.get("client_ref", {}).get("project_id")
            if isinstance(body.get("client_ref"), dict)
            else None
        )
        project = self.access.project(principal, project_id, "jobs:write")["payload"]
        reject_managed(project)
        return self.planning.create(principal, body, project)

    def preflight(
        self,
        principal,
        project,
        shot_id,
        *,
        expected_version=None,
        seed_override=None,
        plan_id=None,
    ):
        project_id = project["id"]
        record = self.access.project(principal, project_id, "jobs:write")
        if expected_version is None and not managed_project(project):
            raise Conflict("document_version_conflict")
        if expected_version is not None and record["version"] != expected_version:
            raise Conflict("document_version_conflict")
        if source_snapshot(project, shot_id) != source_snapshot(
            record["payload"], shot_id
        ):
            raise Conflict("shot_version_conflict")
        current_capabilities = self.capabilities_provider()
        render_project, defaults_version = project, None
        if not managed_project(project):
            from .storyboard_versions import preferences, effective_project

            setting = preferences(self.repo, self.settings.tenant_id, principal.owner)
            defaults_version = setting["version"]
            render_project, _ = effective_project(project, shot_id, setting["defaults"])
        draft, _ = read_draft(
            render_project, shot_id, capabilities=current_capabilities
        )
        if any(draft["inputs"].values()):
            self.access.project(principal, project_id, "assets:read")

        def derive(asset_id, start, end):
            self.access.project(principal, project_id, "assets:write")
            self.assets.get(principal.owner, asset_id, project_id)
            receipt = self.assets.derive(principal.owner, asset_id, start, end)
            if receipt["status"] != "ready":
                raise Conflict("asset_not_ready")
            return receipt["asset_id"]

        def validate_frame(asset_id, artifact_id):
            # Editable document provenance is a preview only. Admission checks
            # the owner/project-scoped immutable receipt before using the image.
            self.access.project(principal, project_id, "assets:read")
            asset = self.assets.get(principal.owner, asset_id, project_id)
            selection = asset.get("selection", {})
            if (
                asset.get("status") != "ready"
                or asset.get("kind") != "image"
                or selection.get("kind") != "artifact_frame"
                or selection.get("position") != "last"
                or selection.get("artifact_id") != artifact_id
            ):
                raise ValueError("上一镜末帧来源未通过素材收据核对")

        body = plan_body(
            render_project,
            shot_id,
            current_capabilities,
            derive,
            validate_frame=validate_frame,
        )
        if seed_override is not None:
            body["controls"]["seed"] = seed_override
        latest = self.access.project(principal, project_id, "jobs:write")
        if expected_version is not None and latest["version"] != expected_version:
            raise Conflict("document_version_conflict")
        if source_snapshot(project, shot_id) != source_snapshot(
            latest["payload"], shot_id
        ):
            raise Conflict("shot_version_conflict")
        if (
            defaults_version is not None
            and preferences(self.repo, self.settings.tenant_id, principal.owner)[
                "version"
            ]
            != defaults_version
        ):
            raise Conflict("storyboard_defaults_changed")
        if defaults_version is None:
            return self.planning.create(principal, body, latest["payload"])
        import copy

        recipe = {
            k: copy.deepcopy(body[k]) for k in ("recipe_id", "prompt", "controls")
        }
        # Keep original receipt/range/audio intent, not only CPU-derived IDs.
        recipe["inputs"] = copy.deepcopy(draft["inputs"])
        if body.get("deployment_profile_id"):
            recipe["deployment_profile_id"] = body["deployment_profile_id"]
        shot = next(e for e in project["entities"] if e["id"] == shot_id)
        from .source_snapshot import source_components

        dependencies = source_components(project, shot_id)
        context = {
            "shot": copy.deepcopy(shot),
            "scene": next(
                (
                    copy.deepcopy(e)
                    for e in project["entities"]
                    if e["id"] == shot.get("parentId")
                ),
                None,
            ),
            "storyboard": copy.deepcopy(
                project.get("journey", {}).get("storyboard", {})
            ),
            "profiles": [
                copy.deepcopy(e)
                for e in dependencies["entities"]
                if e["type"] in {"character", "location", "prop"}
            ],
            "links": copy.deepcopy(dependencies["links"]),
            "global_defaults": copy.deepcopy(setting["defaults"]),
        }
        return self.planning.create(
            principal,
            body,
            latest["payload"],
            plan_id=plan_id,
            authoring_recipe=recipe,
            authoring_context=context,
            defaults_version=defaults_version,
        )

    def create(
        self, principal, plan_id, key, *, initial_status=None, business_identity=None
    ):
        plan = self.access.plan(principal, plan_id)
        project = self.access.project(principal, plan["project_id"], "jobs:write")[
            "payload"
        ]
        if business_identity is None:
            reject_managed(project)
        elif not managed_project(project):
            raise Conflict("quick_chat_projection_required")
        task_scope = self.task_scope(principal, plan["project_id"], business_identity)
        # Replaying a lost response keeps the immutable original even if the
        # editable source has since changed. The ledger compares request hashes.
        existing = self.repo.lookup_job_by_idempotency(task_scope, key)
        if existing:
            return self.repo.create_job(task_scope, plan_id, key)
        self.planning.check_source(project, plan["request"], principal)
        execution = plan["execution_plan"]
        if execution.get("budget_error_code") == "budget_exceeded":
            raise BudgetExceeded("budget_exceeded")
        enabled_setting = (
            self.settings.render_enabled
            if plan["request"]["recipe_id"] == RENDER_RECIPE
            else self.settings.generation_enabled
        )
        ready = (
            enabled_setting
            and execution.get("enabled")
            and execution.get("quote_known")
        )
        status = initial_status or (
            execution.get("admission_state", "queued") if ready else "blocked"
        )
        if status in {"queued", "planned", "waiting_capacity"} and not ready:
            status = "blocked"
        budgets = self.policies.ensure_current(plan, task_scope) if ready else ()
        return self.repo.create_job(
            task_scope,
            plan_id,
            key,
            initial_status=status,
            budget_account_ids=budgets,
            spending_actor=principal.actor_id,
        )

    def create_planned(self, principal, plan_id, business_identity):
        return self.create(
            principal,
            plan_id,
            business_identity,
            initial_status="planned",
            business_identity=business_identity,
        )

    def enqueue(
        self,
        principal,
        job,
        *,
        business=False,
        confirmation_required=False,
        spending_settings_version=None,
    ):
        plan = self.access.plan(principal, job["plan_id"])
        project = self.access.project(principal, job["project_id"], "jobs:write")[
            "payload"
        ]
        if not business:
            reject_managed(project)
        elif not managed_project(project) or job["actor_id"] != "quick-chat-execution":
            raise Conflict("quick_chat_execution_required")
        self.planning.check_source(project, plan["request"], principal)
        task_scope = self.task_scope(
            principal, job["project_id"], job["id"] if business else None
        )
        budgets = self.policies.ensure_current(plan, task_scope)
        return self.repo.enqueue(
            task_scope,
            job["id"],
            budget_account_ids=budgets,
            spending_actor=principal.actor_id,
            confirmation_required=confirmation_required,
            spending_settings_version=spending_settings_version,
        )

    def refresh_planned(self, principal, job_id, plan_id):
        job = self.repo.get_job_for_owner(
            self.settings.tenant_id, principal.owner, job_id
        )
        project = self.access.project(principal, job["project_id"], "jobs:write")[
            "payload"
        ]
        if not managed_project(project) or job["actor_id"] != "quick-chat-execution":
            raise Conflict("quick_chat_execution_required")
        plan = self.access.plan(principal, plan_id)
        if plan["project_id"] != job["project_id"]:
            raise Conflict("plan_not_available")
        self.planning.check_source(project, plan["request"], principal)
        task_scope = self.task_scope(principal, job["project_id"], job_id)
        self.policies.ensure_current(plan, task_scope)
        return self.repo.refresh_unadmitted_plan(task_scope, job_id, plan_id)
