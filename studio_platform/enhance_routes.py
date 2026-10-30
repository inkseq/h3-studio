"""POST /v1/enhance-plans (#795): plan an enhance like a chapter render plan.

The job then starts through the normal /v1/jobs route with the plan id, so
credit holds, spending limits and settlement are the reviewed generation
paths. Users see credits on the usage bar only, never money.
"""

from fastapi import HTTPException, Request

from .enhance_plans import compile_enhance
from .generation_admission import reject_managed
from .inference.outputs import _shape
from .repository import request_hash


def register_routes(app):
    @app.post("/v1/enhance-plans", status_code=201)
    def make_enhance_plan(request: Request, body: dict):
        state = request.app.state
        principal = request.state.principal
        ref = body.get("client_ref") if isinstance(body, dict) else None
        project_id = ref.get("project_id") if isinstance(ref, dict) else None
        project = state.authorized_project(principal, project_id, "jobs:write")[
            "payload"
        ]
        reject_managed(project)
        scope = state.scope(principal, project_id)

        def resolve(entity, kind):
            source = state.resolve_render_source(principal, project_id, entity, kind)
            # The source job's recipe and frame size decide what may be enhanced.
            job = state.repository.get_job(scope, source["source_job_id"])
            try:
                width, height, _, _ = _shape(job)
            except Exception:
                width = height = None
            ref = job["request"].get("client_ref") or {}
            return {
                **source,
                "recipe_id": job["request"].get("recipe_id"),
                "width": width,
                "height": height,
                # The enhanced take joins the source take's shot (its versions list).
                "shot": {k: ref[k] for k in ("shot_id", "shot_version") if k in ref},
                # The take's own prompt guides the regenerate pass (#795 runs).
                "prompt": (job["request"].get("request") or {}).get("prompt"),
            }

        try:
            compiled, display = compile_enhance(body, project, resolve)
        except ValueError as error:
            raise HTTPException(422, str(error)) from None
        policies = state.generation_admission.policies
        admission = policies.evaluate(compiled, scope, request_hash(compiled))
        from .credits_pricing import public_quote
        from .credits_pricing import quote as quote_credits

        repo, settings = state.repository, state.settings
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
        plan = repo.create_plan(
            scope,
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
            "enhance": {k: v for k, v in display.items() if k != "credits"},
            **public_quote(admission.execution),
            "expires_at": plan["expires_at"],
            "blockers": admission.execution["blockers"],
            "execution": state.generation_read.execution(admission.execution),
            "simulation": compiled["simulation"],
        }
