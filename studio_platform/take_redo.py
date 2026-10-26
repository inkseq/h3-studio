"""Read-only prompt diffs anchored to actual marks and an immutable take recipe."""

import copy
import re

from fastapi import Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, model_validator
from sqlalchemy import select

from .quality_views import selection
from .repository import Conflict, NotFound, Scope, jobs, request_hash
from .take_reviews import TakeMark, TakeReviews

PATH = "/v1/projects/{project_id}/shots/{shot_id}/takes/{artifact_id}/redo-preview"


class PromptEdit(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    mark_id: StrictStr = Field(min_length=1, max_length=100)
    start: StrictInt = Field(ge=0)
    end: StrictInt = Field(gt=0)
    replacement: StrictStr = Field(max_length=12000)

    @model_validator(mode="after")
    def bounds(self):
        if self.start >= self.end:
            raise ValueError("redo_text_range_invalid")
        return self


class RedoPreview(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    expected_project_version: StrictInt = Field(ge=1)
    expected_review_revision: StrictInt = Field(ge=0)
    edits: list[PromptEdit] = Field(default_factory=list, max_length=50)


def contract():
    return {
        "path": PATH,
        "method": "POST",
        "access": "projects:read and jobs:read for the same owned project; no new scope.",
        "body": "{expected_project_version,expected_review_revision,edits:[{mark_id,start,end,replacement}]}; max50 edits. Empty edits reads the baseline. Offsets are Unicode code points in the ORIGINAL prompt, not bytes or JavaScript UTF-16 units.",
        "semantics": "Only actual change marks; suggested-only/keep/missing IDs refuse. Nonempty disjoint text spans, changed replacements, no chosen change range overlapping a keep range. Untouched prompt spans and every non-prompt recipe byte stay identical. Unknown historical recipe/seed and PW window recipes refuse rather than reconstruct mutable defaults.",
        "result": "Creator-safe baseline/candidate recipe, prompt diffs with mark ranges, hashes and version binding. needs_edits/no_change_marks/preview; never an executable plan. Reference availability and current capability compatibility remain unchecked until normal preflight.",
        "effect": "Read-only snapshot: no key/receipt, storage mutation, model call, generation, charge or automatic apply. 409 on stale project/review. Read again after edits; do not auto-increment versions.",
        "confirm": "Human confirms the diff, then use existing authoring CAS and fresh generation preflight/confirmation. This slice adds no apply endpoint. Same seed/settings do not guarantee pixel-identical kept ranges; marks are instructions, not an inpainting mask.",
    }


def frozen_recipe(stored):
    recipe = stored.get("authoring_recipe")
    if (
        not isinstance(recipe, dict)
        or set(recipe)
        - {"recipe_id", "deployment_profile_id", "prompt", "controls", "inputs"}
        or not isinstance(recipe.get("recipe_id"), str)
        or not isinstance(recipe.get("prompt"), str)
        or not recipe["prompt"].strip()
        or len(recipe["prompt"]) > 12000
        or not isinstance(recipe.get("controls"), dict)
        or not isinstance(recipe.get("inputs"), dict)
    ):
        raise Conflict("redo_historical_recipe_unknown")
    effective = stored.get("request", {})
    if not isinstance(effective, dict):
        raise Conflict("redo_seed_proof_unknown")
    seed = recipe["controls"].get("seed")
    actual = effective.get("seed")
    if (
        not isinstance(seed, str)
        or not re.fullmatch(r"[0-9]{1,20}", seed)
        or type(actual) is not int
        or not 0 <= actual < 2**64
        or int(seed) != actual
    ):
        raise Conflict("redo_seed_proof_unknown")
    if (
        "script_window_prompts" in recipe["controls"]
        or "script_window_prompts" in effective
        or "window_seconds" in effective
        or effective.get("multi_prompts_gen_type") == "PW"
        or stored.get("script_windows")
    ):
        raise Conflict("redo_window_recipe_requires_explicit_resplit")
    return copy.deepcopy(recipe)


def prompt_diff(recipe, marks, edits):
    by_id = {m["id"]: m for m in marks}
    prompt, cursor, pieces, diff = recipe["prompt"], 0, [], []
    for edit in sorted(edits, key=lambda value: (value.start, value.end)):
        mark = by_id.get(edit.mark_id)
        if not mark or mark["verdict"] != "change":
            raise Conflict("redo_change_mark_required")
        if any(
            other["verdict"] == "keep"
            and max(mark["start_s"], other["start_s"])
            < min(mark["end_s"], other["end_s"])
            for other in marks
        ):
            raise Conflict("redo_keep_change_overlap")
        if edit.start < cursor or edit.end > len(prompt):
            raise ValueError("redo_text_range_invalid")
        original = prompt[edit.start : edit.end]
        if original == edit.replacement:
            raise ValueError("redo_text_unchanged")
        pieces.extend((prompt[cursor : edit.start], edit.replacement))
        cursor = edit.end
        diff.append(
            {
                "mark_id": edit.mark_id,
                "start_s": mark["start_s"],
                "end_s": mark["end_s"],
                "start": edit.start,
                "end": edit.end,
                "before": original,
                "after": edit.replacement,
            }
        )
    pieces.append(prompt[cursor:])
    candidate = copy.deepcopy(recipe)
    candidate["prompt"] = "".join(pieces)
    if not candidate["prompt"].strip() or len(candidate["prompt"]) > 12000:
        raise ValueError("redo_prompt_invalid")
    return candidate, diff


class TakeRedo:
    def __init__(self, repo, tenant):
        self.repo, self.tenant = repo, tenant
        self.reviews = TakeReviews(repo, tenant)

    def preview(self, principal, project_id, shot_id, artifact_id, body):
        parsed = RedoPreview.model_validate(body)
        if not principal.allows(project_id, "jobs:read"):
            raise NotFound("take_not_found")
        with self.reviews._read_snapshot() as conn:
            source = self.reviews._source(
                conn, principal, project_id, shot_id, artifact_id, write=False
            )
            project = self.reviews._row(
                conn,
                Scope(self.tenant, principal.owner, "__projects"),
                "project",
                project_id,
            )
            review = self.reviews._row(
                conn,
                Scope(self.tenant, principal.owner, project_id),
                "take_review",
                artifact_id,
            )
            revision = review["version"] if review else 0
            if project["version"] != parsed.expected_project_version:
                raise Conflict("redo_project_version_conflict")
            if revision != parsed.expected_review_revision:
                raise Conflict("redo_review_revision_conflict")
            stored = conn.execute(
                select(jobs.c.request).where(
                    jobs.c.id == source["job_id"],
                    jobs.c.tenant_id == self.tenant,
                    jobs.c.owner_id == principal.owner,
                    jobs.c.project_id == project_id,
                )
            ).scalar_one()
            recipe = frozen_recipe(stored)
            # Current marks only; suggested sets and whole-take verdict are independent.
            marks = copy.deepcopy(review["payload"].get("marks", []) if review else [])
            marks = [TakeMark.model_validate(mark).model_dump() for mark in marks]
            if len(marks) > 50 or len({m["id"] for m in marks}) != len(marks):
                raise Conflict("redo_review_invalid")
            candidate, diff = prompt_diff(recipe, marks, parsed.edits)
            binding = {
                "project_id": project_id,
                "shot_id": shot_id,
                "artifact_id": artifact_id,
                "job_id": source["job_id"],
                "source_shot_id": source["source_shot"],
                "project_version": project["version"],
                "review_revision": revision,
                "recipe_hash": request_hash(recipe),
            }
        non_prompt = {k: v for k, v in recipe.items() if k != "prompt"}
        return {
            "status": "preview"
            if diff
            else "needs_edits"
            if any(m["verdict"] == "change" for m in marks)
            else "no_change_marks",
            "binding": binding,
            "source_hash": request_hash(binding),
            "candidate_hash": request_hash(candidate),
            # Always use the creator view, even for admins. Never return raw job/context.
            "baseline": selection(recipe),
            "candidate": selection(candidate),
            "marks": marks,
            "diff": diff,
            "preserved": {
                "seed": recipe["controls"]["seed"],
                "non_prompt_hash": request_hash(non_prompt),
            },
            "applied": False,
            "executable": False,
            "preflight_status": "not_checked",
            "pixel_preservation": "not_guaranteed",
        }


def register_routes(app):
    service = TakeRedo(app.state.repository, app.state.settings.tenant_id)
    app.state.take_redo = service

    @app.post(PATH)
    def preview(
        project_id: str,
        shot_id: str,
        artifact_id: str,
        body: RedoPreview,
        request: Request,
    ):
        return JSONResponse(
            service.preview(
                request.state.principal,
                project_id,
                shot_id,
                artifact_id,
                body.model_dump(),
            ),
            headers={"Cache-Control": "no-store"},
        )
