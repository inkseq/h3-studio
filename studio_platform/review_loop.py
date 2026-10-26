"""Read-only, content-free review-loop coverage over original scoped receipts.

No inferred verdicts, invoice settlement, analytics writes or provider calls.
The first slice can prove shipped first takes; explicit review/command receipt
writers and shared-cost allocation are separate, not fabricated from activity.
"""

from collections import defaultdict
from datetime import datetime, timezone
import math

from fastapi import HTTPException, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError

from .identity import require_admin
from .repository import artifacts, attempts, documents, identifier, jobs

MAX_JOBS = 2000
MAX_ATTEMPTS = 8000
MAX_ARTIFACTS = 4000
MAX_FILMS = 200
MAX_REVIEWS = 8000


def _utc(value):
    return (
        datetime.fromtimestamp(value, timezone.utc).isoformat().replace("+00:00", "Z")
    )


def _metric(numerator, denominator, reasons=()):
    reasons = sorted(set(reasons))
    if not denominator:
        reasons.append("no_eligible_shots")
    return {
        "value": numerator / denominator if denominator and not reasons else None,
        "numerator": numerator,
        "denominator": denominator,
        "status": "partial"
        if reasons and denominator
        else ("insufficient_data" if reasons else "complete"),
        "reasons": reasons,
    }


def _bounded(connection, statement, limit, reasons, label):
    rows = list(connection.execute(statement.limit(limit + 1)).mappings())
    if len(rows) > limit:
        reasons.add(label + "_limit")
    return rows[:limit]


def review_loop(repo, tenant, owner, *, start, end, now):
    identifier(owner)
    if not all(type(n) in (int, float) and math.isfinite(n) for n in (start, end, now)):
        raise ValueError("review_loop_interval_invalid")
    if not 0 <= start < end <= now or end - start > 90 * 86400:
        raise ValueError("review_loop_interval_invalid")
    reasons = set()
    with repo.engine.connect() as connection:
        # A single stable read snapshot; never serialize mutable customer state.
        if connection.dialect.name == "postgresql":
            connection = connection.execution_options(isolation_level="REPEATABLE READ")
            connection.begin()
            connection.exec_driver_sql("SET TRANSACTION READ ONLY")
            connection.exec_driver_sql("SET LOCAL statement_timeout = '3000ms'")
        else:
            connection.exec_driver_sql("BEGIN")
        scoped = (jobs.c.tenant_id == tenant, jobs.c.owner_id == owner)
        # Read bounded lifetime history, not just the date window: otherwise an
        # old shot's latest redo could be misrepresented as its first generation.
        rows = _bounded(
            connection,
            select(
                jobs.c.id,
                jobs.c.project_id,
                jobs.c.created_at,
                jobs.c.status,
                jobs.c.current_attempt_id,
                jobs.c.result["artifact_ids"].label("accepted_artifacts"),
                jobs.c.request["client_ref"]["shot_id"].as_string().label("shot"),
                jobs.c.request["client_ref"]["project_id"]
                .as_string()
                .label("ref_project"),
            )
            .where(
                *scoped,
                jobs.c.created_at <= now,
                jobs.c.request["request"]["mode"].as_string().in_(("fl", "ref")),
            )
            .order_by(jobs.c.created_at, jobs.c.id),
            MAX_JOBS,
            reasons,
            "jobs",
        )
        groups = defaultdict(list)
        unresolved = 0
        for row in rows:
            if not row["shot"] or row["ref_project"] != row["project_id"]:
                unresolved += 1
                continue
            groups[(row["project_id"], row["shot"])].append(row)
        if unresolved:
            reasons.add("generation_lineage_missing")
        cohort = {
            key: value
            for key, value in groups.items()
            if start <= value[0]["created_at"] < end
        }
        by_job = {row["id"]: key for key, value in cohort.items() for row in value}
        selected = {row["id"]: row for value in cohort.values() for row in value}
        project_ids = sorted({key[0] for key in cohort})
        # Migration aliases require verified receipt mapping, not editable hints.
        # This initial projection refuses a complete logical-shot rate for these
        # projects until that mapping is integrated; it never silently doubles it.
        migrated = (
            list(
                connection.execute(
                    select(documents.c.project_id)
                    .where(
                        documents.c.tenant_id == tenant,
                        documents.c.owner_id == owner,
                        documents.c.project_id.in_(project_ids),
                        documents.c.kind == "quick_chat_story_migration",
                    )
                    .distinct()
                    .limit(MAX_JOBS + 1)
                ).scalars()
            )
            if project_ids
            else []
        )
        if migrated:
            reasons.add("migration_lineage_unresolved")
        attempt_rows = (
            _bounded(
                connection,
                select(
                    attempts.c.id,
                    attempts.c.job_id,
                    attempts.c.status,
                    attempts.c.fence,
                    attempts.c.actual_cost_microusd,
                )
                .select_from(attempts.join(jobs, jobs.c.id == attempts.c.job_id))
                .where(
                    *scoped,
                    jobs.c.id.in_(list(by_job)),
                    attempts.c.created_at <= now,
                )
                .order_by(attempts.c.id),
                MAX_ATTEMPTS,
                reasons,
                "attempts",
            )
            if by_job
            else []
        )
        output_rows = (
            _bounded(
                connection,
                select(
                    artifacts.c.id,
                    artifacts.c.job_id,
                    artifacts.c.attempt_id,
                    artifacts.c.created_at,
                )
                .select_from(artifacts.join(jobs, artifacts.c.job_id == jobs.c.id))
                .where(
                    *scoped,
                    jobs.c.id.in_(list(by_job)),
                    artifacts.c.created_at <= now,
                    artifacts.c.metadata["kind"].as_string() == "video",
                )
                .order_by(artifacts.c.created_at, artifacts.c.id),
                MAX_ARTIFACTS,
                reasons,
                "artifacts",
            )
            if by_job
            else []
        )
        review_reasons = set()
        review_rows = (
            _bounded(
                connection,
                select(
                    documents.c.project_id,
                    documents.c.payload["artifact_id"].as_string().label("artifact_id"),
                    documents.c.payload["source_job_id"].as_string().label("job_id"),
                    documents.c.payload["source_shot_id"].as_string().label("shot_id"),
                    documents.c.payload["response"]["revision"]
                    .as_integer()
                    .label("revision"),
                    documents.c.payload["response"]["verdict"]
                    .as_string()
                    .label("verdict"),
                    documents.c.payload["response"]["actor_kind"]
                    .as_string()
                    .label("actor_kind"),
                )
                .where(
                    documents.c.tenant_id == tenant,
                    documents.c.owner_id == owner,
                    documents.c.project_id.in_(project_ids),
                    documents.c.kind == "take_review_receipt",
                    documents.c.payload["contract_version"].as_integer() == 1,
                    documents.c.payload["verdict_written"].as_boolean().is_(True),
                    documents.c.payload["artifact_id"]
                    .as_string()
                    .in_([r["id"] for r in output_rows]),
                    documents.c.updated_at <= now,
                )
                .order_by(documents.c.updated_at, documents.c.document_id),
                MAX_REVIEWS,
                review_reasons,
                "reviews",
            )
            if output_rows
            else []
        )
        film_rows = (
            _bounded(
                connection,
                select(
                    documents.c.project_id,
                    documents.c.document_id,
                    documents.c.payload["results"].label("results"),
                    documents.c.payload["export"].label("export"),
                )
                .where(
                    documents.c.tenant_id == tenant,
                    documents.c.owner_id == owner,
                    documents.c.project_id.in_(project_ids),
                    documents.c.kind == "film_run",
                    documents.c.updated_at <= now,
                    documents.c.payload["phase"].as_string() == "completed",
                )
                .order_by(documents.c.project_id, documents.c.document_id),
                MAX_FILMS,
                reasons,
                "films",
            )
            if project_ids
            else []
        )
        export_ids = [
            row["export"].get("artifact_id")
            for row in film_rows
            if isinstance(row["export"], dict)
        ]
        # Validate final export proof against the same tenant/owner ledger, not
        # an unvalidated ID embedded in a document. No content URLs are selected.
        export_rows = (
            list(
                connection.execute(
                    select(
                        artifacts.c.id,
                        artifacts.c.job_id,
                        artifacts.c.created_at,
                        jobs.c.project_id,
                        jobs.c.result["artifact_ids"].label("accepted_artifacts"),
                    )
                    .select_from(
                        artifacts.join(jobs, artifacts.c.job_id == jobs.c.id).join(
                            attempts, artifacts.c.attempt_id == attempts.c.id
                        )
                    )
                    .where(
                        *scoped,
                        artifacts.c.id.in_(export_ids),
                        artifacts.c.created_at <= now,
                        artifacts.c.metadata["kind"].as_string() == "video",
                        jobs.c.status == "succeeded",
                        attempts.c.status == "succeeded",
                        attempts.c.job_id == jobs.c.id,
                        attempts.c.id == jobs.c.current_attempt_id,
                    )
                ).mappings()
            )
            if export_ids
            else []
        )

    attempt_map = {row["id"]: row for row in attempt_rows}
    delivered = defaultdict(list)
    artifact_map = {}
    for row in output_rows:
        job, attempt = selected[row["job_id"]], attempt_map.get(row["attempt_id"])
        if (
            attempt
            and attempt["job_id"] == job["id"]
            and job["status"] == attempt["status"] == "succeeded"
            and attempt["id"] == job["current_attempt_id"]
            and row["id"] in (job["accepted_artifacts"] or [])
        ):
            key = by_job[row["job_id"]]
            delivered[key].append(row)
            artifact_map[row["id"]] = (key, row)
    for candidates in delivered.values():
        if (
            len(candidates) > 1
            and candidates[0]["created_at"] == candidates[1]["created_at"]
        ):
            reasons.add("first_delivery_order_ambiguous")
    proven_jobs = {row["job_id"] for values in delivered.values() for row in values}
    if any(
        job["status"] == "succeeded" and ident not in proven_jobs
        for ident, job in selected.items()
    ):
        reasons.add("delivery_receipt_unverified")
    exports = {
        row["id"]: row
        for row in export_rows
        if row["id"] in (row["accepted_artifacts"] or [])
    }
    appearances = defaultdict(list)
    invalid_films = 0
    for film in film_rows:
        receipt = film["export"] if isinstance(film["export"], dict) else {}
        output = exports.get(receipt.get("artifact_id"))
        if (
            not output
            or output["project_id"] != film["project_id"]
            or output["job_id"] != receipt.get("job_id")
            or not isinstance(film["results"], dict)
        ):
            invalid_films += 1
            continue
        for result in film["results"].values():
            ident = result.get("artifact_id") if isinstance(result, dict) else None
            source = artifact_map.get(ident)
            if source is None:
                # Imported/other-cohort media is not a generated cohort take.
                continue
            key, candidate = source
            if (
                key[0] != film["project_id"]
                or candidate["created_at"] > output["created_at"]
            ):
                invalid_films += 1
                continue
            appearances[key].append((output["created_at"], ident))
    if invalid_films:
        reasons.add("export_receipt_unverified")
    shipped = 0
    for key, values in appearances.items():
        values.sort()
        finished, ident = values[0]
        if len({take for when, take in values if when == finished}) > 1:
            reasons.add("first_export_order_ambiguous")
            continue
        # A redo is a new original request after the first take was delivered.
        # Initial concurrent batch alternatives and retries are not inferred to
        # be content-driven redo; neither implies a human usability verdict.
        first = delivered[key][0]
        if ident == first["id"] and not any(
            first["created_at"] <= row["created_at"] <= finished
            for row in cohort[key]
            if row["id"] != first["job_id"]
        ):
            shipped += 1
    costs = [
        row["actual_cost_microusd"]
        for row in attempt_rows
        if type(row["actual_cost_microusd"]) is int and row["actual_cost_microusd"] >= 0
    ]
    # The current whole-take decision is the latest explicit revision, including
    # retraction. A later range-only edit must not attribute an earlier human
    # decision to an agent. No review text or mutable selection is read here.
    verdicts = {}
    for row in review_rows:
        source = artifact_map.get(row["artifact_id"])
        if (
            not source
            or source[0] != (row["project_id"], row["shot_id"])
            or source[1]["job_id"] != row["job_id"]
            or type(row["revision"]) is not int
            or row["revision"] < 1
            or row["verdict"] not in ("keep", "redo", None)
            or row["actor_kind"] not in ("human", "agent")
        ):
            review_reasons.add("review_receipt_unverified")
            continue
        prior = verdicts.get(row["artifact_id"])
        if prior is None or prior["revision"] < row["revision"]:
            verdicts[row["artifact_id"]] = row
        elif prior["revision"] == row["revision"]:
            review_reasons.add("review_revision_ambiguous")
    first_verdicts = [
        verdicts[values[0]["id"]]
        for values in delivered.values()
        if values[0]["id"] in verdicts
    ]
    human_first = [
        r
        for r in first_verdicts
        if r["actor_kind"] == "human" and r["verdict"] is not None
    ]
    agent_first = [
        r
        for r in first_verdicts
        if r["actor_kind"] == "agent" and r["verdict"] is not None
    ]
    usable_shots = {
        source[0]
        for ident, source in artifact_map.items()
        if ident in verdicts
        and verdicts[ident]["actor_kind"] == "human"
        and verdicts[ident]["verdict"] == "keep"
    }
    explicit_reasons = reasons | review_reasons
    usable_reasons = reasons | review_reasons
    if not human_first:
        explicit_reasons.add("review_verdicts_missing")
    if not usable_shots:
        usable_reasons.add("review_verdicts_missing")
    return {
        "schema_version": 1,
        "owner_id": owner,
        "from": _utc(start),
        "to": _utc(end),
        "as_of": _utc(now),
        "cohort": {
            "observed_shots": len(cohort),
            "delivered_shots": len(delivered),
            "exported_shots": len(appearances),
            "complete": not reasons,
        },
        "metrics": {
            "first_take_shipped_rate": _metric(shipped, len(delivered), reasons),
            "first_usable_rate": _metric(
                sum(r["verdict"] == "keep" for r in human_first),
                len(human_first),
                explicit_reasons,
            ),
            "full_cost_per_usable_shot_microusd": _metric(
                None,
                len(usable_shots),
                usable_reasons | {"shared_cost_allocation_missing"},
            ),
            "user_actions_per_usable_shot": _metric(
                None, len(usable_shots), usable_reasons | {"command_lineage_missing"}
            ),
        },
        "coverage": {
            "reviewed_first_candidates": len(human_first),
            "agent_reviewed_first_candidates": len(agent_first),
            "human_usable_shots": len(usable_shots),
            "whole_take_verdicts_available": True,
            "review_reasons": sorted(review_reasons),
            "known_attempt_cost_microusd": sum(costs) if costs else None,
            "attempts_with_known_cost": len(costs),
            "attempts_observed": len(attempt_rows),
            "cost_components_complete": False,
            "command_lineage_complete": False,
            "unresolved_generation_rows": unresolved,
            "migrated_projects": len(migrated),
            "unverified_films": invalid_films,
            "reasons": sorted(reasons),
        },
    }


def register_routes(app):
    @app.get("/v1/operator/admin/review-loop")
    def get_review_loop(
        request: Request,
        owner_id: str = Query(..., min_length=1, max_length=200),
        start: datetime = Query(..., alias="from"),
        end: datetime = Query(..., alias="to"),
    ):
        repo, settings = app.state.repository, app.state.settings
        require_admin(
            request.state.principal,
            repo,
            settings,
            machine_code="review_loop_human_required",
        )
        if any(
            value.tzinfo is None or value.utcoffset().total_seconds() != 0
            for value in (start, end)
        ):
            raise HTTPException(400, "review_loop_utc_required")
        try:
            result = review_loop(
                repo,
                settings.tenant_id,
                owner_id,
                start=start.timestamp(),
                end=end.timestamp(),
                now=repo.clock(),
            )
        except ValueError:
            raise HTTPException(400, "review_loop_interval_invalid") from None
        except DBAPIError as error:
            if getattr(error.orig, "sqlstate", None) == "57014":
                raise HTTPException(503, "review_loop_query_timeout") from None
            raise
        return JSONResponse(result, headers={"Cache-Control": "no-store"})
