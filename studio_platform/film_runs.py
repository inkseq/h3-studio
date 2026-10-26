"""Frozen film orchestration receipts over the original plans/jobs/artifacts.

There is no film job ledger. All executable work uses the existing batch and
generation admission services; a document only records their original IDs.
"""

from __future__ import annotations

import copy
import math
import re
import uuid

from sqlalchemy import insert, inspect, select, update
from .render_limits import MAX_AUDIO_TRACKS
from .generation_services import freeze_generation_policy

from .repository import (
    Conflict,
    NotFound,
    Scope,
    documents,
    jobs,
    plans,
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


def accepted_policy_admission(connection, repo, scope, plan, key):
    """Only an exact original accepted Film batch keeps its input authorization.

    This is not authority for an arbitrary copy of a Film plan. Normal account,
    grant, source, budget and cancellation gates still run in the caller.
    """
    if not isinstance(key, str):
        return False
    matched = re.fullmatch(r"batch:([0-9a-f-]{36}):(0|[1-9][0-9]{0,2})", key)
    if matched is None:
        return False
    try:
        batch_id, index = matched[1], int(matched[2])
        if str(uuid.UUID(batch_id)) != batch_id or any(
            plan[field] != expected
            for field, expected in (
                ("tenant_id", scope.tenant_id),
                ("owner_id", scope.owner_id),
                ("project_id", scope.project_id),
            )
        ):
            return False
        compiled = plan["request"]
        value = validate_film_source(connection, repo, scope, compiled)
        accepted = value.get("accepted_at")
        if (
            type(accepted) not in (int, float)
            or not math.isfinite(accepted)
            or not 0 < accepted <= repo.clock()
            or not isinstance(value.get("submission_hash"), str)
            or re.fullmatch(r"[0-9a-f]{64}", value["submission_hash"]) is None
            or value.get("id") != compiled["film_source_ref"]["run_id"]
            or value.get("project_id") != scope.project_id
            or value.get("plan_hashes", {}).get(plan["id"]) != request_hash(compiled)
        ):
            return False
        from .batches import batches

        if not inspect(connection).has_table(batches.name):
            return False  # Optional older offline fixtures do not acquire DDL.
        row = (
            connection.execute(
                select(batches).where(
                    batches.c.id == batch_id,
                    batches.c.tenant == scope.tenant_id,
                    batches.c.owner == scope.owner_id,
                    batches.c.project_id == scope.project_id,
                    batches.c.actor_id == scope.actor_id,
                )
            )
            .mappings()
            .first()
        )
        if row is None or row["cancel_requested"] is not None:
            return False
        chosen = {
            value["id"] + ":shots": value["generation"],
            value["id"] + ":chapters": value["chapters"],
            value["id"] + ":export": [value["export"]],
        }.get(row["idempotency_key"])
        if not isinstance(chosen, list) or not 1 <= len(chosen) <= 100:
            return False
        expected = [item["plan_id"] for item in chosen]
        if (
            not all(isinstance(item, str) and item for item in expected)
            or len(set(expected)) != len(expected)
            or not 0 <= index < len(expected)
            or expected[index] != plan["id"]
            or not isinstance(row["items"], list)
            or [item["plan_id"] for item in row["items"]] != expected
            or row["fingerprint"]
            != request_hash({"project_id": scope.project_id, "plan_ids": expected})
        ):
            return False
        return True
    except (KeyError, TypeError, ValueError, Conflict, NotFound):
        # An old/malformed/unaccepted record falls back to today's policy.
        return False


def frozen_story(project, results, *, chapters=None):
    """Apply an exact run's chosen results to a COPY, never the editable Story."""
    value = copy.deepcopy(project)
    if chapters is not None:
        # The original accepted header owns inclusion. Older receipts may have
        # ignored an authoring filmCuts draft: never reinterpret that work after
        # a release. Only this transient copy receives the frozen membership.
        value.setdefault("journey", {})["filmCuts"] = {
            chapter["chapter_id"]: {
                "shotIds": list(chapter["shot_ids"]),
                **(
                    {"clip_sound_crossfade_s": chapter["clip_sound_crossfade_s"]}
                    if "clip_sound_crossfade_s" in chapter
                    else {}
                ),
            }
            for chapter in chapters
        }
    entities = {item["id"]: item for item in value["entities"]}
    for shot_id, result in results.items():
        shot = entities[shot_id]
        if result.get("entity"):
            entity = copy.deepcopy(result["entity"])
            from .shot_segments import has_segments, segment_frames

            if has_segments(shot):
                if not result.get("reused"):
                    raise Conflict("film_segments_source_requires_review")
                try:
                    segment_frames(shot, entity)
                except ValueError:
                    raise Conflict("film_segments_source_requires_review") from None
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
            result = self.public(value, principal)
            transport = getattr(self.repo, "generation_transport", None)
            if (
                transport is not None
                and value.get("accepted_at") is not None
                and value["phase"] not in TERMINAL
                and not value["cancel_requested"]
                and value["phase"] not in {"blocked", "authorization_required"}
                and transport.film_status(
                    conn, self.tenant, principal.owner, project_id, run_id
                )
                == "ERROR"
            ):
                # An unexpected failed SDK step may not have updated the
                # domain receipt. Expose explicit same-run resume without a
                # read-side mutation, exception text, automatic epoch or retry.
                result.update(
                    phase="blocked",
                    error_code="film_workflow_failed",
                    failed_phase=value["phase"],
                )
        return result

    @staticmethod
    def public(value, principal=None):
        from .credits_pricing import aggregate_quotes, user_estimate

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
        if value.get("render_contract_version") == 5:
            result["render_contract_version"] = 5
        result["counts"].update(
            generated_completed=sum(
                item["shot_id"] in value["results"] for item in value["generation"]
            ),
            chapters_completed=sum(
                bool(item["artifact_id"]) for item in value["chapters"]
            ),
            export_completed=int(bool(value["export"]["artifact_id"])),
        )
        credits = aggregate_quotes(value["generation"]) or value.get(
            "credit_summary", {}
        )
        result.update(credits)
        hide_dollars = bool(credits) and (
            principal is None or principal.machine or principal.role != "admin"
        )
        result["estimate"] = user_estimate(
            result["estimate"], hide_dollars=hide_dollars
        )
        return result

    def preflight(self, principal, project_id, body, key):
        from .credits_pricing import public_quote

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
            return self.public(old, principal)
        except NotFound:
            pass
        if record["version"] != body["expected_version"]:
            raise Conflict("document_version_conflict")
        project = copy.deepcopy(record["payload"])
        journey = project.get("journey", {})
        sound = journey.get("sound") if isinstance(journey, dict) else None
        mode = sound.get("mode") if isinstance(sound, dict) else None
        if not isinstance(mode, str) or mode not in {
            "silent",
            "dialogue",
            "music",
            "mixed",
        }:
            raise Conflict("film_sound_plan_required")
        chapters = sorted(
            (item for item in project["entities"] if item["type"] == "chapter"),
            key=lambda item: item["order"],
        )
        if not 1 <= len(chapters) <= 50:
            raise ValueError("film_chapter_count_limit")
        from .render_timing import (
            crossfade,
            enabled,
            expanded_audio_count,
            has_speed,
            segment_timing,
            shot_frames,
        )
        from .shot_segments import has_segments

        render_version = (
            5
            if any(
                enabled(
                    project, chapter["id"], ordered_chapter(project, chapter["id"])[2]
                )
                for chapter in chapters
            )
            else 3
        )
        missing, results, chapter_rows, warnings = [], {}, [], []
        total_seconds = 0
        for chapter in chapters:
            _, _, shots = ordered_chapter(project, chapter["id"])
            if not 1 <= len(shots) <= 50:
                raise ValueError("film_chapter_shot_count_limit")
            if (
                sum(
                    len(segment_timing(shot)) if has_segments(shot) else 1
                    for shot in shots
                )
                > 50
            ):
                raise ValueError("film_chapter_clip_count_limit")
            if (
                any(has_segments(shot) for shot in shots)
                and expanded_audio_count(project, chapter["id"], shots)
                > MAX_AUDIO_TRACKS
            ):
                raise ValueError("film_audio_track_count_limit")
            if options["burn_subtitles"]:
                from .caption_server import caption_signature, compile_subtitles

                frames = (
                    sum(shot_frames(shot)[1] for shot in shots)
                    if render_version == 5
                    else sum(
                        max(
                            1,
                            math.floor(
                                shot.get("data", {}).get("seconds", 5) * 24 + 0.5
                            ),
                        )
                        for shot in shots
                    )
                )
                _, _, caption_blockers, _ = compile_subtitles(
                    project, chapter["id"], shots, frames, render_version=render_version
                )
                if caption_blockers:
                    track = (
                        project.get("journey", {})
                        .get("captionTracks", {})
                        .get(chapter["id"], {})
                    )
                    try:
                        confirmed = isinstance(track, dict) and track.get(
                            "confirmedSnapshot"
                        ) == caption_signature(
                            project, chapter["id"], shots, render_version=render_version
                        )
                    except (KeyError, TypeError, ValueError):
                        confirmed = False
                    if confirmed:
                        raise Conflict("film_captions_invalid")
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
            if render_version == 5:
                chapter_rows[-1]["clip_sound_crossfade_s"] = crossfade(
                    project, chapter["id"]
                )
                chapter_rows[-1]["timing"] = [
                    {
                        "shot_id": shot["id"],
                        "source_frames": shot_frames(shot)[0],
                        "frames": shot_frames(shot)[1],
                        "playback_rate": shot_frames(shot)[2],
                    }
                    for shot in shots
                ]
            for shot in shots:
                from .render_plans import finite

                seconds = shot.get("data", {}).get("seconds", 5)
                if not finite(seconds, 1 / 24, 600):
                    raise ValueError("film_shot_duration_invalid")
                total_seconds += (
                    shot_frames(shot)[1] / 24 if render_version == 5 else seconds
                )
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
                    if has_segments(shot):
                        raise Conflict("film_segments_source_requires_review")
                    if has_speed(shot):
                        raise Conflict("film_speed_source_requires_review")
                    missing.append(shot["id"])
                    continue
                try:
                    source = self.app.state.resolve_render_source(
                        principal, project_id, selected, "video"
                    )
                    if has_segments(shot):
                        segment_timing(shot, selected, source["duration"])
                    elif has_speed(shot):
                        from .render_plans import video_range

                        selected_range = shot["data"]["selectedVideoRange"]
                        original = {
                            **shot,
                            "data": {
                                **shot["data"],
                                "selectedVideoRange": {
                                    key: value
                                    for key, value in selected_range.items()
                                    if key != "playback_rate"
                                },
                            },
                        }
                        video_range(
                            original, selected, source["duration"], shot_frames(shot)[0]
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
                    if has_segments(shot):
                        raise Conflict("film_segments_source_requires_review") from None
                    if has_speed(shot):
                        raise Conflict("film_speed_source_requires_review") from None
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
                run_id + "-shots",
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
                execution = copy.deepcopy(plan["execution_plan"])
                freeze_generation_policy(
                    self.repo, self.tenant, principal.owner, compiled, execution
                )
                frozen = self.repo.create_plan(
                    self.access.scope(principal, project_id),
                    compiled,
                    execution,
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
                        **public_quote(frozen["execution_plan"]),
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
        if render_version == 5:
            value["render_contract_version"] = 5
        if not generation:
            from .credits import credit_policy

            with self.repo.engine.connect() as connection:
                policy = credit_policy(connection, self.tenant)
            if policy["enabled"]:
                value["credit_summary"] = {
                    "credits": 0,
                    "price_revision": policy["active_revision"],
                    "price_revisions": [policy["active_revision"]],
                    "breakdown": [],
                }
        self.repo.put_document(
            film_scope(self.tenant, principal.owner, project_id),
            KIND,
            run_id,
            value,
            expected_version=0,
        )
        return self.public(value, principal)

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
            from .identity import lock_account_gate

            lock_account_gate(conn)
            self.repo._content_write_guard(
                conn, film_scope(self.tenant, principal.owner, project_id)
            )
            row = self._row(conn, principal, project_id, run_id, lock=True)
            value = copy.deepcopy(row["payload"])
            if not value.get("submission_hash"):
                scope = self.access.scope(principal, project_id)
                plan_ids = [item["plan_id"] for item in value["generation"]]
                if len(set(plan_ids)) != len(plan_ids) or set(
                    value["plan_hashes"]
                ) != set(plan_ids):
                    raise Conflict("film_source_changed")
                for plan_id in plan_ids:
                    plan = (
                        conn.execute(
                            select(plans).where(
                                self.repo._scope(plans, scope), plans.c.id == plan_id
                            )
                        )
                        .mappings()
                        .first()
                    )
                    if plan is None or value["plan_hashes"][plan_id] != request_hash(
                        plan["request"]
                    ):
                        raise Conflict("film_source_changed")
                    # No batch key exists at parent acceptance: today's policy
                    # must authorize every original child before it is frozen.
                    self.repo._check_generation_policy(
                        conn, scope, plan, admitting=True
                    )
            accept(value)
            self._write(conn, principal, project_id, run_id, value, row)
            if self.enqueue is None:
                raise Conflict("film_transport_unavailable")
            self.enqueue(conn, self.tenant, principal.owner, project_id, run_id)
        return self.public(value, principal)

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
        return self.public(value, principal)

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
        return self.public(value, principal)

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
        from .credits_pricing import quote as quote_credits

        with self.repo.engine.connect() as connection:
            credit_quote = quote_credits(
                connection,
                self.tenant,
                principal.owner,
                compiled,
                execution=admission.execution,
            )
        if credit_quote is not None:
            admission.execution["credit_quote"] = credit_quote
        freeze_generation_policy(
            self.repo, self.tenant, principal.owner, compiled, admission.execution
        )
        digest = request_hash(compiled)
        self.repo.create_plan(
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
        authoring = frozen_story(value["project"], {}, chapters=value["chapters"])
        project = frozen_story(authoring, results)
        for chapter in value["chapters"]:
            if not chapter["plan_id"]:
                options = {
                    key: item
                    for key, item in value["options"].items()
                    if item is not None
                }
                burn_subtitles = options.pop("burn_subtitles", False)
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
                    contract_version=value.get("render_contract_version", 3),
                )
                if burn_subtitles:
                    from .caption_server import compile_subtitles
                    from .render_plans import render_source_hash

                    # Consent belongs to the accepted authoring snapshot. Only
                    # this run's selected media changes in the render copy;
                    # never manufacture a new confirmation for those results.
                    original_shots = ordered_chapter(authoring, chapter["chapter_id"])[
                        2
                    ]
                    subtitles, _, caption_blockers, _ = compile_subtitles(
                        authoring,
                        chapter["chapter_id"],
                        original_shots,
                        compiled["output_spec"]["frame_count"],
                        render_version=value.get("render_contract_version", 3),
                    )
                    blockers.extend(caption_blockers)
                    compiled["request"]["render"]["subtitles"] = subtitles
                    compiled["server_source_hash"] = render_source_hash(
                        project,
                        chapter["chapter_id"],
                        burn_subtitles=True,
                        contract_version=value.get("render_contract_version", 3),
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
                        if item["chapter_id"] == chapter["chapter_id"]  # noqa: B023 -- called synchronously in this iteration
                    )
                    if target["artifact_id"] not in {None, result["artifact_id"]}:  # noqa: B023 -- called synchronously in this iteration
                        raise Conflict("film_result_identity_conflict")
                    target["artifact_id"] = result["artifact_id"]  # noqa: B023 -- called synchronously in this iteration

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
                    "simulation": job["request"].get("simulation", False),  # noqa: B023 -- called synchronously in this iteration
                    "object": {
                        "key": meta["object_key"],
                        "size_bytes": meta["size_bytes"],
                        "sha256": meta["sha256"],
                        "content_type": meta.get("content_type", meta.get("mime")),
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
            if value.get("render_contract_version") == 5:
                chapter_results[-1]["render_version"] = 5
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
