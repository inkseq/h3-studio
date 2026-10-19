"""Frozen film orchestration receipts over the original plans/jobs/artifacts.

There is no film job ledger. All executable work uses the existing batch and
generation admission services; a document only records their original IDs.
"""

from __future__ import annotations

import copy
import uuid

from sqlalchemy import insert, select, update

from .repository import (
    Conflict,
    NotFound,
    Scope,
    documents,
    jobs,
    identifier,
    request_hash,
)
from .render_plans import compile_render, compile_film_export, ordered_chapter

KIND = "film_run"
EXECUTION_BLOCKS = frozenset(
    {
        "film_render_input_blocked",
        "spending_confirmation_required",
        "spending_settings_changed",
        "spending_usage_unconfirmed",
        "user_daily_limit_reached",
        "connection_daily_limit_reached",
        "budget_exceeded",
        "execution_policy_changed_or_unavailable",
        "capacity_approval_unavailable",
    }
)
VERSION = "inkseq-film-v1"
TERMINAL = frozenset({"completed", "cancelled"})
JOB_TERMINAL = frozenset({"succeeded", "failed", "cancelled"})


def actor_grant(principal):
    """Private grant identity only, not a bearer token or copied permission set."""
    return {
        key: getattr(principal, key)
        for key in (
            "actor_id",
            "machine",
            "auth_mode",
            "password_version",
            "session_hash",
        )
    }


def film_scope(tenant, owner, project):
    return Scope(tenant, owner, project)


def validate_film_source(connection, repo, scope, compiled):
    """Final source fence, called under the original owner/admission gate.

    Only an exact server-created plan registered in a frozen accepted header may
    use this authority. The caller must retain its normal project/trash/asset and
    spending checks. Editable Story changes do not rewrite the accepted snapshot.
    """
    ref = compiled.get("film_source_ref")
    if not isinstance(ref, dict) or set(ref) != {"run_id", "snapshot_hash"}:
        raise Conflict("film_source_invalid")
    row = (
        connection.execute(
            select(documents).where(
                repo._scope(
                    documents,
                    film_scope(scope.tenant_id, scope.owner_id, scope.project_id),
                ),
                documents.c.kind == KIND,
                documents.c.document_id == ref["run_id"],
            )
        )
        .mappings()
        .first()
    )
    if not row:
        raise Conflict("film_source_unavailable")
    value = row["payload"]
    if (
        value.get("snapshot_hash") != ref["snapshot_hash"]
        or value.get("actor_id") != scope.actor_id
        or value.get("cancel_requested") is True
        or request_hash(value["project"]) != value["snapshot_hash"]
        or compiled.get("client_ref", {}).get("project_id") != scope.project_id
        or value.get("phase") in {"preflight", "cancel_requested", "cancelled"}
        or request_hash(compiled) not in value.get("plan_hashes", {}).values()
    ):
        raise Conflict("film_source_changed")
    return value


def frozen_story(project, results):
    """Apply an exact run's chosen results to a COPY, never the editable Story."""
    value = copy.deepcopy(project)
    entities = {item["id"]: item for item in value["entities"]}
    for shot_id, result in results.items():
        shot = entities[shot_id]
        if result.get("entity"):
            entity = copy.deepcopy(result["entity"])
            if entity["id"] in entities and entities[entity["id"]] != entity:
                raise Conflict("film_result_identity_conflict")
            if entity["id"] not in entities:
                value["entities"].append(entity)
                entities[entity["id"]] = entity
            shot.setdefault("data", {})["selectedAssetId"] = entity["id"]
            if not result.get("reused"):
                shot["data"].pop("selectedVideoRange", None)
    return value


def selected_recipe_current(project, shot_id, original, defaults, capabilities):
    """Compare effective authoring inputs, excluding selection-only mutations.

    Historical receipts without an immutable recipe are unknown, not authority
    to purchase a replacement for an explicitly selected, authorized take.
    """
    recipe = original.get("authoring_recipe")
    if not isinstance(recipe, dict):
        return None
    from .generation_draft import plan_body, read_draft
    from .storyboard_versions import effective_project

    try:
        rendered, _ = effective_project(project, shot_id, defaults)
        draft, issues = read_draft(
            rendered,
            shot_id,
            recipes=capabilities["recipes"],
            capabilities=capabilities,
        )
        if issues:
            return False
        # Only derive a comparison: retain original receipt/range intent below.
        # Actual preflight and admission still perform all normal validation.
        body = plan_body(
            rendered,
            shot_id,
            capabilities,
            lambda asset_id, start, end: asset_id,
            validate_frame=lambda *args: None,
        )
        current = {
            key: copy.deepcopy(body[key]) for key in ("recipe_id", "prompt", "controls")
        }
        current["inputs"] = copy.deepcopy(draft["inputs"])
        if body.get("deployment_profile_id"):
            current["deployment_profile_id"] = body["deployment_profile_id"]
        if draft["controls"].get("seed") in (None, ""):
            current["controls"]["seed"] = recipe.get("controls", {}).get("seed")
        elif current["controls"].get("seed") is not None:
            current["controls"]["seed"] = str(current["controls"]["seed"])
        return request_hash(current) == request_hash(recipe)
    except (ValueError, KeyError, NotFound, Conflict):
        return False


class FilmRuns:
    def __init__(self, app, *, enqueue=None):
        self.app = app
        self.repo = app.state.repository
        self.tenant = app.state.settings.tenant_id
        self.access = app.state.generation_access
        self.enqueue = enqueue

    def execution_context(self, tenant, owner, project_id, run_id):
        """Controller-only input to its existing Auth grant revalidator.

        This is not an authentication API and never constructs a Principal.
        Browser sessions must still exist/unexpired with current account/mode;
        keys/clients must pass their original live grants/revocation/pause gates.
        A refused future admission does not cancel/retry any accepted job.
        """
        if tenant != self.tenant:
            raise NotFound("film_not_found")
        record = self.repo.get_document(
            film_scope(tenant, owner, project_id), KIND, run_id
        )
        return copy.deepcopy(record["payload"]["actor_grant"])

    def record_execution_block(self, tenant, owner, project_id, run_id, code):
        """Controller-only finite metadata; never reconstruct admission grants.

        A revoked grant cannot start/cancel work through this path. An already
        requested cancellation may finish only by observing its own original
        terminal jobs, whose native reconciliation continues independently.
        """
        if tenant != self.tenant:
            raise NotFound("film_not_found")
        for ident in (tenant, owner, project_id, run_id):
            identifier(ident)
        if code != "film_actor_grant_unavailable":
            raise ValueError("film_execution_block_invalid")
        scope = film_scope(tenant, owner, project_id)
        with self.repo.transaction() as conn:
            self.repo._content_write_guard(conn, scope)
            row = self.repo._locked(
                conn,
                select(documents).where(
                    self.repo._scope(documents, scope),
                    documents.c.kind == KIND,
                    documents.c.document_id == run_id,
                ),
            )
            if row is None:
                raise NotFound("film_not_found")
            value = copy.deepcopy(row["payload"])
            if (
                value["workflow_version"] != VERSION
                or value["project_id"] != project_id
            ):
                raise Conflict("film_workflow_version_mismatch")
            if value.get("accepted_at") is None:
                raise Conflict("film_confirmation_required")
            if value["phase"] not in TERMINAL:
                if value["cancel_requested"]:
                    job_ids = {
                        item["job_id"]
                        for item in [
                            *value["generation"],
                            *value["chapters"],
                            value["export"],
                        ]
                        if item.get("job_id")
                    }
                    states = (
                        dict(
                            conn.execute(
                                select(jobs.c.id, jobs.c.status).where(
                                    self.repo._scope(jobs, scope),
                                    jobs.c.id.in_(job_ids),
                                )
                            ).all()
                        )
                        if job_ids
                        else {}
                    )
                    value["phase"] = (
                        "cancelled"
                        if set(states) == job_ids
                        and all(state in JOB_TERMINAL for state in states.values())
                        else "cancel_requested"
                    )
                else:
                    value.update(phase="authorization_required", error_code=code)
                if value != row["payload"]:
                    self._write_scoped(conn, scope, run_id, value, row)
        return {
            "terminal": value["phase"] in TERMINAL,
            "phase": value["phase"],
            "error_code": value.get("error_code"),
            "wait_seconds": 10,
        }

    def _row(self, conn, principal, project_id, run_id, *, lock=False):
        statement = select(documents).where(
            self.repo._scope(
                documents, film_scope(self.tenant, principal.owner, project_id)
            ),
            documents.c.kind == KIND,
            documents.c.document_id == run_id,
        )
        row = (
            self.repo._locked(conn, statement)
            if lock
            else conn.execute(statement).mappings().first()
        )
        if not row:
            raise NotFound("film_not_found")
        return row

    def _write(self, conn, principal, project_id, run_id, payload, row=None):
        scope = film_scope(self.tenant, principal.owner, project_id)
        return self._write_scoped(conn, scope, run_id, payload, row)

    def _write_scoped(self, conn, scope, run_id, payload, row=None):
        self.repo._content_write_guard(conn, scope, payload)
        if (
            len(__import__('json').dumps(payload, ensure_ascii=False).encode())
            > self.app.state.settings.max_project_bytes
        ):
            raise Conflict("film_document_size_limit")
        if row is None:
            conn.execute(
                insert(documents).values(
                    tenant_id=scope.tenant_id,
                    owner_id=scope.owner_id,
                    project_id=scope.project_id,
                    kind=KIND,
                    document_id=run_id,
                    payload=payload,
                    version=1,
                    updated_at=self.repo.clock(),
                )
            )
        else:
            conn.execute(
                update(documents)
                .where(
                    self.repo._scope(documents, scope),
                    documents.c.kind == KIND,
                    documents.c.document_id == run_id,
                    documents.c.version == row["version"],
                )
                .values(
                    payload=payload,
                    version=row["version"] + 1,
                    updated_at=self.repo.clock(),
                )
            )

    def _mutate(self, principal, project_id, run_id, change):
        self.access.project(principal, project_id, "jobs:write")
        with self.repo.transaction() as conn:
            self.repo._content_write_guard(
                conn, film_scope(self.tenant, principal.owner, project_id)
            )
            row = self._row(conn, principal, project_id, run_id, lock=True)
            value = copy.deepcopy(row["payload"])
            change(value)
            if value != row["payload"]:
                self._write(conn, principal, project_id, run_id, value, row)
        return value

    def get(self, principal, project_id, run_id):
        self.access.project(principal, project_id, "jobs:read")
        with self.repo.engine.connect() as conn:
            value = self._row(conn, principal, project_id, run_id)["payload"]
        return self.public(value)

    @staticmethod
    def public(value):
        # Do not return frozen object keys, runtime configuration or private inputs.
        result = {
            key: copy.deepcopy(value.get(key))
            for key in (
                "id",
                "project_id",
                "project_version",
                "snapshot_hash",
                "workflow_version",
                "phase",
                "created_at",
                "accepted_at",
                "cancel_requested",
                "error_code",
                "generation",
                "chapters",
                "export",
                "estimate",
                "options",
                "counts",
                "warnings",
            )
        }
        result["counts"].update(
            generated_completed=sum(
                item["shot_id"] in value["results"] for item in value["generation"]
            ),
            chapters_completed=sum(
                bool(item["artifact_id"]) for item in value["chapters"]
            ),
            export_completed=int(bool(value["export"]["artifact_id"])),
        )
        return result

    def preflight(self, principal, project_id, body, key):
        identifier(key)
        if (
            type(body) is not dict
            or set(body)
            - {"expected_version", "resolution", "aspect", "burn_subtitles"}
            or type(body.get("expected_version")) is not int
            or body["expected_version"] < 1
        ):
            raise ValueError("film_preflight_invalid")
        options = {
            "resolution": body.get("resolution", "720P"),
            "aspect": body.get("aspect"),
            "burn_subtitles": body.get("burn_subtitles", False),
        }
        from .render_plans import DIMENSIONS

        if (
            options["resolution"] not in DIMENSIONS
            or type(options["burn_subtitles"]) is not bool
            or options["aspect"] is not None
            and options["aspect"] not in DIMENSIONS[options["resolution"]]
        ):
            raise ValueError("film_output_options_invalid")
        record = self.access.project(principal, project_id, "jobs:write")
        from .generation_admission import reject_managed

        reject_managed(record["payload"])
        run_id = "film-" + request_hash(
            {
                "tenant": self.tenant,
                "owner": principal.owner,
                "actor": principal.actor_id,
                "project": project_id,
                "key": key,
            }
        )
        fingerprint = request_hash(body)
        try:
            with self.repo.engine.connect() as conn:
                old = self._row(conn, principal, project_id, run_id)["payload"]
            if (
                old["fingerprint"] != fingerprint
                or old["actor_id"] != principal.actor_id
            ):
                raise Conflict("film_idempotency_conflict")
            return self.public(old)
        except NotFound:
            pass
        if record["version"] != body["expected_version"]:
            raise Conflict("document_version_conflict")
        project = copy.deepcopy(record["payload"])
        chapters = sorted(
            (item for item in project["entities"] if item["type"] == "chapter"),
            key=lambda item: item["order"],
        )
        if not 1 <= len(chapters) <= 50:
            raise ValueError("film_chapter_count_limit")
        missing, results, chapter_rows, warnings = [], {}, [], []
        total_seconds = 0
        for chapter in chapters:
            _, _, shots = ordered_chapter(project, chapter["id"])
            if not 1 <= len(shots) <= 50:
                raise ValueError("film_chapter_shot_count_limit")
            if options["burn_subtitles"]:
                from .caption_server import compile_subtitles

                frames = sum(
                    round(shot.get("data", {}).get("seconds", 5) * 24) for shot in shots
                )
                _, _, caption_blockers, _ = compile_subtitles(
                    project, chapter["id"], shots, frames
                )
                if caption_blockers:
                    raise Conflict("film_captions_require_confirmation")
            chapter_rows.append(
                {
                    "chapter_id": chapter["id"],
                    "shot_ids": [s["id"] for s in shots],
                    "plan_id": None,
                    "job_id": None,
                    "artifact_id": None,
                }
            )
            for shot in shots:
                from .render_plans import finite

                seconds = shot.get("data", {}).get("seconds", 5)
                if not finite(seconds, 1 / 24, 600):
                    raise ValueError("film_shot_duration_invalid")
                total_seconds += seconds
                selected = next(
                    (
                        entity
                        for entity in project["entities"]
                        if entity["id"] == shot.get("data", {}).get("selectedAssetId")
                        and entity["type"] == "video"
                    ),
                    None,
                )
                if selected is None:
                    missing.append(shot["id"])
                    continue
                try:
                    source = self.app.state.resolve_render_source(
                        principal, project_id, selected, "video"
                    )
                    if source.get("source_job_id"):
                        from .storyboard_versions import preferences

                        original = self.access.job(principal, source["source_job_id"])
                        freshness = selected_recipe_current(
                            project,
                            shot["id"],
                            original["request"],
                            preferences(self.repo, self.tenant, principal.owner)[
                                "defaults"
                            ],
                            self.app.state.generation_admission.capabilities_provider(),
                        )
                        if freshness is False:
                            raise Conflict("film_selected_take_outdated")
                        if freshness is None:
                            warnings.append("film_selected_recipe_unknown")
                    results[shot["id"]] = {
                        "entity": selected,
                        "reused": True,
                        "artifact_id": source.get("artifact_id"),
                        "source_hash": request_hash(source),
                    }
                except (NotFound, Conflict, ValueError):
                    missing.append(shot["id"])
        if len(missing) > 100 or total_seconds > 600:
            raise ValueError("film_size_limit")
        snapshot_hash = request_hash(project)
        generation, hashes, estimate, quote_known = [], {}, 0, True
        if missing:
            preview = self.app.state.storyboard_versions.preflight(
                principal,
                project_id,
                {
                    "expected_version": record["version"],
                    "shot_ids": missing,
                    "copies": 1,
                },
                run_id + ":shots",
            )
            if preview.get("blocked"):
                raise Conflict("film_generation_preflight_blocked")
            original = self.repo.get_document(
                film_scope(self.tenant, principal.owner, project_id),
                "storyboard_preflight",
                preview["id"],
            )["payload"]
            for item in original["items"]:
                plan = self.repo.get_plan(
                    film_scope(self.tenant, principal.owner, project_id),
                    item["plan_id"],
                )
                plan_id = str(
                    uuid.uuid5(uuid.NAMESPACE_URL, run_id + ":shot:" + item["shot_id"])
                )
                compiled = copy.deepcopy(plan["request"])
                compiled["film_source_ref"] = {
                    "run_id": run_id,
                    "snapshot_hash": snapshot_hash,
                }
                frozen = self.repo.create_plan(
                    self.access.scope(principal, project_id),
                    compiled,
                    plan["execution_plan"],
                    expires_at=plan["expires_at"],
                    estimated_cost_microusd=plan["estimated_cost_microusd"],
                    plan_id=plan_id,
                )
                hashes[plan_id] = request_hash(compiled)
                estimate += plan["estimated_cost_microusd"]
                quote_known = (
                    quote_known and plan["execution_plan"].get("quote_known") is True
                )
                generation.append(
                    {
                        "shot_id": item["shot_id"],
                        "plan_id": frozen["id"],
                        "source_plan_id": item["plan_id"],
                        "seed": item["seed"],
                        "job_id": None,
                    }
                )
        latest = self.access.project(principal, project_id, "jobs:write")
        if (
            latest["version"] != record["version"]
            or request_hash(latest["payload"]) != snapshot_hash
        ):
            raise Conflict("document_version_conflict")
        value = {
            "id": run_id,
            "project_id": project_id,
            "project_version": record["version"],
            "snapshot_hash": snapshot_hash,
            "project": project,
            "actor_id": principal.actor_id,
            "actor_grant": actor_grant(principal),
            "fingerprint": fingerprint,
            "workflow_version": VERSION,
            "phase": "preflight",
            "created_at": self.repo.clock(),
            "accepted_at": None,
            "cancel_requested": False,
            "error_code": None,
            "options": options,
            "warnings": sorted(set(warnings)),
            "generation": generation,
            "results": results,
            "chapters": chapter_rows,
            "export": {"plan_id": None, "job_id": None, "artifact_id": None},
            "plan_hashes": hashes,
            "estimate": {
                "cost_microusd": estimate if quote_known else None,
                "quote_known": quote_known,
                "final_bill": False,
            },
            "counts": {
                "shots": len(results) + len(missing),
                "reused": len(results),
                "generate": len(missing),
            },
        }
        self.repo.put_document(
            film_scope(self.tenant, principal.owner, project_id),
            KIND,
            run_id,
            value,
            expected_version=0,
        )
        return self.public(value)

    def submit(self, principal, project_id, body, key):
        identifier(key)
        if (
            type(body) is not dict
            or set(body) - {"preflight_id", "confirm", "spending_settings_version"}
            or body.get("confirm") is not True
        ):
            raise ValueError("film_confirmation_required")
        run_id = identifier(body.get("preflight_id"))
        self.access.project(principal, project_id, "jobs:write")
        with self.repo.engine.connect() as conn:
            initial = self._row(conn, principal, project_id, run_id)["payload"]
        if initial["actor_id"] != principal.actor_id:
            raise Conflict("film_actor_mismatch")
        if not initial.get("submission_hash"):
            from .spending_limits import check_submission_confirmation

            # No nested transaction while holding the original owner write gate.
            # Batch admission rechecks/consumes these exact original grants.
            check = check_submission_confirmation(
                self.repo,
                self.tenant,
                principal,
                [item["plan_id"] for item in initial["generation"]],
            )
            version = body.get("spending_settings_version", check.version)
            if type(version) is not int or version != check.version:
                raise Conflict("spending_settings_changed")

        def accept(value):
            if value["actor_id"] != principal.actor_id:
                raise Conflict("film_actor_mismatch")
            digest = request_hash({"body": body, "key": key})
            if value.get("submission_hash"):
                if value["submission_hash"] != digest:
                    raise Conflict("film_idempotency_conflict")
                return
            if value["cancel_requested"] or value["phase"] != "preflight":
                raise Conflict("film_not_submittable")
            value.update(
                phase="generating",
                accepted_at=self.repo.clock(),
                submission_hash=digest,
            )

        # Confirmation does no provider work. Enqueue is committed with its header.
        self.access.project(principal, project_id, "jobs:write")
        with self.repo.transaction() as conn:
            self.repo._content_write_guard(
                conn, film_scope(self.tenant, principal.owner, project_id)
            )
            row = self._row(conn, principal, project_id, run_id, lock=True)
            value = copy.deepcopy(row["payload"])
            accept(value)
            self._write(conn, principal, project_id, run_id, value, row)
            if self.enqueue is None:
                raise Conflict("film_transport_unavailable")
            self.enqueue(conn, self.tenant, principal.owner, project_id, run_id)
        return self.public(value)

    def cancel(self, principal, project_id, run_id):
        value = self._mutate(
            principal,
            project_id,
            run_id,
            lambda value: (
                value.update(cancel_requested=True, phase="cancel_requested")
                if value["phase"] not in TERMINAL
                else None
            ),
        )
        for item in [*value["generation"], *value["chapters"], value["export"]]:
            if item.get("job_id"):
                self.repo.request_cancel(
                    self.access.scope(principal, project_id), item["job_id"]
                )
        return self.public(value)

    def resume(self, principal, project_id, run_id):
        def resume(value):
            if value["phase"] in TERMINAL or value["cancel_requested"]:
                raise Conflict("film_not_resumable")
            if value.get("accepted_at") is None:
                raise Conflict("film_confirmation_required")
            if value["actor_id"] != principal.actor_id:
                raise Conflict("film_actor_mismatch")
            # Explicit same-actor resume may renew a browser session, never
            # manufacture owner-wide grants for a paused/revoked Agent key.
            value["actor_grant"] = actor_grant(principal)
            value["error_code"] = None
            if value["phase"] in {"authorization_required", "blocked"}:
                value["phase"] = "generating"

        value = self._mutate(principal, project_id, run_id, resume)
        # The deterministic workflow ID wakes the original run, never new jobs.
        if self.enqueue is None:
            raise Conflict("film_transport_unavailable")
        with self.repo.transaction() as conn:
            self.enqueue(conn, self.tenant, principal.owner, project_id, run_id)
        return self.public(value)

    def _admit(self, principal, value, items, suffix):
        pending = [
            item
            for item in items
            if not item.get("job_id")
            or self.access.job(principal, item["job_id"])["status"]
            in {"planned", "blocked"}
        ]
        if not pending:
            return value
        result = self.app.state.submit_generation_batch(
            principal,
            {
                "client_project_id": value["project_id"],
                "plan_ids": [item["plan_id"] for item in items],
            },
            value["id"] + ":" + suffix,
        )
        links = {item["plan_id"]: item.get("job_id") for item in result["items"]}

        def link(current):
            target = (
                current["generation"]
                if suffix == "shots"
                else current["chapters"]
                if suffix == "chapters"
                else [current["export"]]
            )
            for item in target:
                if links.get(item["plan_id"]):
                    if item.get("job_id") not in {None, links[item["plan_id"]]}:
                        raise Conflict("film_job_identity_conflict")
                    item["job_id"] = links[item["plan_id"]]

        return self._mutate(principal, value["project_id"], value["id"], link)

    def _result(self, principal, project_id, job_id):
        job = self.access.job(principal, job_id)
        if job["status"] != "succeeded":
            return job["status"], None
        videos = [
            item
            for item in self.repo.list_artifacts(
                self.access.scope(principal, project_id), job_id
            )
            if item["metadata"].get("kind") == "video"
        ]
        if len(videos) != 1:
            raise Conflict("film_video_result_unconfirmed")
        artifact = self.access.artifact(principal, videos[0]["id"])
        entity = {
            "id": "film-take-" + artifact["id"],
            "type": "video",
            "title": "Film take",
            "version": 1,
            "order": 0,
            "parentId": None,
            "description": "",
            "status": "ready",
            "data": {"cloudArtifactId": artifact["id"]},
        }
        return "succeeded", {"artifact_id": artifact["id"], "entity": entity}

    def _render_plan(self, principal, value, label, compiled, blockers):
        if blockers:
            raise Conflict("film_render_input_blocked")
        compiled["film_source_ref"] = {
            "run_id": value["id"],
            "snapshot_hash": value["snapshot_hash"],
        }
        plan_id = str(uuid.uuid5(uuid.NAMESPACE_URL, value["id"] + ":" + label))
        digest = request_hash(compiled)
        admission = self.app.state.generation_planning.policies.evaluate(
            compiled, self.access.scope(principal, value["project_id"]), digest
        )
        plan = self.repo.create_plan(
            self.access.scope(principal, value["project_id"]),
            compiled,
            admission.execution,
            expires_at=admission.expires_at,
            estimated_cost_microusd=0,
            plan_id=plan_id,
        )

        def register(current):
            current["plan_hashes"][plan_id] = digest
            target = (
                current["export"]
                if label == "export"
                else next(
                    item for item in current["chapters"] if item["chapter_id"] == label
                )
            )
            target["plan_id"] = plan_id

        return self._mutate(principal, value["project_id"], value["id"], register)

    def _waiting(self, principal, value, phase, error_code=None):
        if value["phase"] != phase or value.get("error_code") != error_code:
            self._mutate(
                principal,
                value["project_id"],
                value["id"],
                lambda current: (
                    current.update(phase=phase, error_code=error_code)
                    if not current["cancel_requested"]
                    else None
                ),
            )
        return {
            "terminal": False,
            "phase": phase,
            "error_code": error_code,
            "wait_seconds": 10,
        }

    def record_business_block(self, principal, project_id, run_id, code):
        """Known refusals wait visibly for same-run resume, never auto-retry work."""
        if code not in EXECUTION_BLOCKS:
            raise ValueError("film_execution_block_invalid")
        self.access.project(principal, project_id, "jobs:write")
        with self.repo.engine.connect() as conn:
            value = copy.deepcopy(
                self._row(conn, principal, project_id, run_id)["payload"]
            )
        if value["phase"] in TERMINAL:
            return {"terminal": True, "phase": value["phase"]}
        if value.get("accepted_at") is None:
            raise Conflict("film_confirmation_required")
        return self._waiting(principal, value, "blocked", code)

    def turn(self, principal, project_id, run_id):
        """One bounded domain turn. The workflow supplies durable waiting."""
        self.access.project(principal, project_id, "jobs:write")
        with self.repo.engine.connect() as conn:
            value = copy.deepcopy(
                self._row(conn, principal, project_id, run_id)["payload"]
            )
        if value["workflow_version"] != VERSION:
            raise Conflict("film_workflow_version_mismatch")
        if value["phase"] in TERMINAL:
            return {"terminal": True, "phase": value["phase"]}
        if value["cancel_requested"]:
            states = []
            for item in [*value["generation"], *value["chapters"], value["export"]]:
                if item.get("job_id"):
                    self.repo.request_cancel(
                        self.access.scope(principal, project_id), item["job_id"]
                    )
                    states.append(self.access.job(principal, item["job_id"])["status"])
            if all(state in JOB_TERMINAL for state in states):
                self._mutate(
                    principal,
                    project_id,
                    run_id,
                    lambda current: current.update(phase="cancelled"),
                )
                return {"terminal": True, "phase": "cancelled"}
            return {"terminal": False, "phase": "cancel_requested", "wait_seconds": 10}
        if value.get("accepted_at") is None:
            raise Conflict("film_confirmation_required")
        if value["phase"] == "blocked" and value.get("error_code") in EXECUTION_BLOCKS:
            return self._waiting(principal, value, "blocked", value["error_code"])
        value = self._admit(principal, value, value["generation"], "shots")
        results = copy.deepcopy(value["results"])
        for item in value["generation"]:
            if not item.get("job_id"):
                return self._waiting(principal, value, "admission_blocked")
            status, result = self._result(principal, project_id, item["job_id"])
            if status in {"failed", "cancelled"}:
                return self._waiting(
                    principal, value, "blocked", "film_original_job_failed"
                )
            if status != "succeeded":
                return self._waiting(principal, value, "generating")
            results[item["shot_id"]] = result
        if results != value["results"] or value["phase"] == "generating":
            value = self._mutate(
                principal,
                project_id,
                run_id,
                lambda current: current.update(results=results, phase="chapters"),
            )
        project = frozen_story(value["project"], results)
        for chapter in value["chapters"]:
            if not chapter["plan_id"]:
                options = {
                    key: item
                    for key, item in value["options"].items()
                    if item is not None
                }
                compiled, _, blockers, _ = compile_render(
                    {
                        "client_ref": {
                            "project_id": project_id,
                            "chapter_id": chapter["chapter_id"],
                        },
                        **options,
                    },
                    project,
                    lambda entity, kind: self.app.state.resolve_render_source(
                        principal, project_id, entity, kind
                    ),
                )
                value = self._render_plan(
                    principal, value, chapter["chapter_id"], compiled, blockers
                )
        value = self._admit(principal, value, value["chapters"], "chapters")
        chapter_results = []
        for chapter in value["chapters"]:
            if not chapter.get("job_id"):
                return self._waiting(principal, value, "admission_blocked")
            status, result = self._result(principal, project_id, chapter["job_id"])
            if status != "succeeded":
                return self._waiting(
                    principal,
                    value,
                    "chapters" if status not in JOB_TERMINAL else "blocked",
                    "film_original_job_failed" if status in JOB_TERMINAL else None,
                )
            if chapter["artifact_id"] != result["artifact_id"]:
                if chapter["artifact_id"] is not None:
                    raise Conflict("film_result_identity_conflict")

                def record_chapter(current):
                    target = next(
                        item
                        for item in current["chapters"]
                        if item["chapter_id"] == chapter["chapter_id"]
                    )
                    if target["artifact_id"] not in {None, result["artifact_id"]}:
                        raise Conflict("film_result_identity_conflict")
                    target["artifact_id"] = result["artifact_id"]

                value = self._mutate(principal, project_id, run_id, record_chapter)
            job = self.access.job(principal, chapter["job_id"])
            records = self.repo.list_artifacts(
                self.access.scope(principal, project_id), job["id"]
            )

            def snapshot(item):
                self.access.artifact(principal, item["id"])
                meta = item["metadata"]
                return {
                    "kind": meta["kind"],
                    "simulation": job["request"].get("simulation", False),
                    "object": {
                        "key": meta["object_key"],
                        "size_bytes": meta["size_bytes"],
                        "sha256": meta["sha256"],
                        "content_type": meta["mime"],
                    },
                }

            chapter_results.append(
                {
                    "chapter_id": chapter["chapter_id"],
                    "output_spec": job["request"]["output_spec"],
                    "video": snapshot(
                        next(
                            item
                            for item in records
                            if item["metadata"]["kind"] == "video"
                        )
                    ),
                    "audio": next(
                        (
                            snapshot(item)
                            for item in records
                            if item["metadata"]["kind"] == "audio"
                        ),
                        None,
                    ),
                }
            )
        if not value["export"]["plan_id"]:
            compiled = compile_film_export(project_id, run_id, chapter_results)
            value = self._render_plan(principal, value, "export", compiled, [])
        value = self._admit(principal, value, [value["export"]], "export")
        if not value["export"].get("job_id"):
            return self._waiting(principal, value, "admission_blocked")
        status, result = self._result(principal, project_id, value["export"]["job_id"])
        if status == "succeeded":

            def finish(current):
                current["export"]["artifact_id"] = result["artifact_id"]
                current["phase"] = "completed"

            self._mutate(principal, project_id, run_id, finish)
            return {"terminal": True, "phase": "completed"}
        return self._waiting(
            principal,
            value,
            "exporting" if status not in JOB_TERMINAL else "blocked",
            "film_original_job_failed" if status in JOB_TERMINAL else None,
        )
