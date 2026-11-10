"""Shared generation admission; HTTP and scenario callers retain their identity.

Extracted from the existing API and aligned with the preserved Quick Chat
admission boundary. This module does not own a queue, permissions or GPU calls.
"""

from typing import NamedTuple

from .capabilities import capabilities
from .generation_draft import plan_body, read_draft
from .repository import Scope, Conflict, BudgetExceeded
from .render_plans import RECIPE as RENDER_RECIPE
from .source_snapshot import source_snapshot
from .inky_hooks import queue_refresh


def managed_project(project):
    return (
        project.get("integration_kind") == "quick_chat"
        or project.get("journey", {}).get("integration_kind") == "quick_chat"
    )


def reject_managed(project):
    if managed_project(project):
        raise Conflict("quick_chat_managed_resource")


class PreflightReads(NamedTuple):
    """Loads shared by every shot of one storyboard preflight (#1102, #1130)."""

    record: dict
    capabilities: dict
    setting: dict


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
        prompt_client=None,
    ):
        self.prompt_client = prompt_client
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

    def model_prompt(self, project, shot_id, draft, body):
        """English full-reference prompt for our own H3 machines (h3_prompt)."""
        # Called by planning after the quality tier picks the engine; a plain
        # story has no profile before that, and the server default is H3.
        profile = (
            body.get("deployment_profile_id")
            or self.settings.default_deployment_profile_id
        )
        if (
            self.settings.execution_backend != "wangp-worker"
            or not str(profile or "").startswith("h3-")
            or body.get("controls", {}).get("script_window_prompts") is not None
        ):
            return None
        if self.prompt_client is None:
            from .story_chat_routes import configured_client

            try:
                self.prompt_client = configured_client(self.settings) or False
            except Exception:
                self.prompt_client = False
        if not self.prompt_client:
            return None
        from .h3_prompt import model_prompt

        return model_prompt(
            self.prompt_client,
            project,
            shot_id,
            {**draft, "controls": body.get("controls", {})},
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
        queue_refresh(principal, project_id)
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
        reads=None,
    ):
        """Plan one shot. ``reads`` carries one storyboard preflight's shared
        loads; the version, shot source and defaults fences still run here."""
        project_id = project["id"]
        if reads is None:
            record = self.access.project(principal, project_id, "jobs:write")
        else:
            self.access.authorize(principal, project_id, "jobs:write")
            record = reads.record
        if expected_version is None and not managed_project(project):
            raise Conflict("document_version_conflict")
        if expected_version is not None and record["version"] != expected_version:
            raise Conflict("document_version_conflict")
        if source_snapshot(project, shot_id) != source_snapshot(
            record["payload"], shot_id
        ):
            raise Conflict("shot_version_conflict")
        queue_refresh(principal, project_id)
        current_capabilities = (
            self.capabilities_provider() if reads is None else reads.capabilities
        )
        render_project, defaults_version = project, None
        if not managed_project(project):
            from .storyboard_versions import preferences, effective_project

            setting = (
                preferences(self.repo, self.settings.tenant_id, principal.owner)
                if reads is None
                else reads.setting
            )
            defaults_version = setting["version"]
            render_project, _ = effective_project(project, shot_id, setting["defaults"])
        from .quality_authoring import project as resolve_quality_project

        render_project = resolve_quality_project(
            self.settings, self.repo, principal, render_project, stored=True
        )
        draft, _ = read_draft(
            render_project, shot_id, capabilities=current_capabilities
        )
        if any(draft["inputs"].values()):
            self.access.authorize(principal, project_id, "assets:read")

        def derive(asset_id, start, end):
            self.access.authorize(principal, project_id, "assets:write")
            self.assets.get(principal.owner, asset_id, project_id)
            receipt = self.assets.derive(principal.owner, asset_id, start, end)
            if receipt["status"] != "ready":
                raise Conflict("asset_not_ready")
            return receipt["asset_id"]

        def validate_frame(asset_id, artifact_id):
            # Editable document provenance is a preview only. Admission checks
            # the owner/project-scoped immutable receipt before using the image.
            self.access.authorize(principal, project_id, "assets:read")
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

        def written(resolved):
            return self.model_prompt(render_project, shot_id, draft, resolved)

        if reads is None:
            latest = self.access.project(principal, project_id, "jobs:write")
        else:
            # An unchanged version is an unchanged payload: every project write
            # bumps it. Re-reading the number skips reloading the whole story.
            version = self.access.project_version(principal, project_id, "jobs:write")
            latest = {"version": version, "payload": record["payload"]}
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
            return self.planning.create(
                principal,
                body,
                latest["payload"],
                stored_selection=True,
                model_prompt=written,
            )
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
            stored_selection=True,
            model_prompt=written,
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
        if status in {"queued", "waiting_capacity"}:
            self._require_no_maintenance()
        budgets = self.policies.ensure_current(plan, task_scope) if ready else ()
        return self.repo.create_job(
            task_scope,
            plan_id,
            key,
            initial_status=status,
            budget_account_ids=budgets,
            spending_actor=principal.actor_id,
        )

    def _require_no_maintenance(self):
        """Release window (#453): refuse new paid work; accepted work continues."""
        from .maintenance_notice import require_open

        with self.repo.engine.connect() as connection:
            require_open(connection, self.settings.tenant_id)

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
        self._require_no_maintenance()
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
