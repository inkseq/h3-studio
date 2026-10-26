"""Explicit take reviews on existing scoped documents; never generation authority."""

import copy
import math
from contextlib import contextmanager, nullcontext
from typing import Literal

from fastapi import Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, model_validator
from sqlalchemy import insert, select, update

from .content_lifecycle import (
    _assert_project_available,
    assert_content_available,
    assert_project_writable,
    owner_reference_gate,
)
from .generation_draft import shot_entity
from .repository import (
    Conflict,
    NotFound,
    Scope,
    artifacts,
    attempts,
    documents,
    jobs,
    request_hash,
)
from .storyboard_versions import migration_source_shots

TAGS = ("performance", "camera", "consistency", "subtitles", "quality", "other")
PATH = "/v1/projects/{project_id}/shots/{shot_id}/takes/{artifact_id}/review"
SUMMARY_PATH = "/v1/projects/{project_id}/shots/{shot_id}/take-reviews"
SUMMARY_LIMIT = 100


class TakeMark(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)
    id: StrictStr = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")
    start_s: float = Field(ge=0)
    end_s: float = Field(gt=0)
    tags: list[
        Literal["performance", "camera", "consistency", "subtitles", "quality", "other"]
    ] = Field(max_length=6)
    verdict: Literal["keep", "change"]
    note: StrictStr = Field(max_length=500)

    @model_validator(mode="after")
    def bounds(self):
        if self.start_s >= self.end_s or len(set(self.tags)) != len(self.tags):
            raise ValueError("take_review_mark_invalid")
        return self


class TakeReviewPatch(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    expected_revision: StrictInt = Field(ge=0)
    verdict: Literal["keep", "redo"] | None = None
    marks: list[TakeMark] = Field(default_factory=list, max_length=50)

    @model_validator(mode="after")
    def edits(self):
        if not self.model_fields_set.intersection({"verdict", "marks"}):
            raise ValueError("take_review_patch_empty")
        if len({m.id for m in self.marks}) != len(self.marks):
            raise ValueError("take_review_duplicate_mark_id")
        return self


def contract():
    return {
        "path": PATH,
        "read": "GET; projects:read. Returns revision, verdict, marks, actor_kind, updated_at.",
        "summaries": {
            "path": SUMMARY_PATH,
            "method": "GET",
            "fields": [
                "artifact_id",
                "revision",
                "verdict",
                "mark_count",
                "actor_kind",
                "updated_at",
            ],
            "semantics": "Newest-first list of eligible accepted video takes; unreviewed revision0/verdictnull/mark_count0/actor_kindnull/updated_atnull. Same scope/provenance/trash fences, no locks, no notes. Empty eligible set returns [].",
            "limit": SUMMARY_LIMIT,
            "overflow": "409 take_review_summary_limit if more than100 candidate videos; no partial list. Use the existing paginated versions listing and individual review GETs in this exceptional case.",
        },
        "write": "PATCH; projects:write + projects:read; Idempotency-Key required.",
        "body": {
            "expected_revision": 0,
            "verdict": "keep",
            "marks": [
                {
                    "id": "mark-1",
                    "start_s": 1.0,
                    "end_s": 2.0,
                    "tags": ["camera"],
                    "verdict": "change",
                    "note": "",
                }
            ],
        },
        "semantics": "At least verdict or marks; omitted field is preserved. verdict:null retracts whole-take evaluation; marks:[] clears ranges. Marks replace the list, max50, unique IDs; notes max500; finite seconds inside immutable video duration; overlap allowed. Tags: "
        + ", ".join(TAGS),
        "identity": "Scoped accepted artifact and original shot (verified migration aliases allowed), independent of selection/star. No imported or unproven take. No automatic verdict from marks, picks, stars, export or suggestions.",
        "recovery": "CAS409 does not mutate. Same key/body replays the small receipt; changed body409. GET full marks; replay still checks trash/purge fences. actor_kind is human/agent from authentication.",
        "privacy": "Notes only in current product review, no immutable receipt/analytics text. Explicit artifact/project purge redacts marks; no generation, charging or provider call.",
    }


class TakeReviews:
    def __init__(self, repo, tenant):
        self.repo, self.tenant = repo, tenant

    def _where(self, scope, kind, ident):
        return (
            self.repo._scope(documents, scope),
            documents.c.kind == kind,
            documents.c.document_id == ident,
        )

    def _row(self, conn, scope, kind, ident):
        return (
            conn.execute(select(documents).where(*self._where(scope, kind, ident)))
            .mappings()
            .first()
        )

    def _source_ids(self, conn, principal, project_id, shot_id, *, write):
        if principal.support_viewer and write:
            raise NotFound("take_not_found")
        if not principal.allows(
            project_id, "projects:write" if write else "projects:read"
        ):
            raise NotFound("take_not_found")
        project_scope = Scope(self.tenant, principal.owner, "__projects")
        query = select(documents).where(
            *self._where(project_scope, "project", project_id)
        )
        if write:
            owner_reference_gate(conn, self.tenant, principal.owner)
            record = self.repo._locked(conn, query)
        else:
            record = conn.execute(query).mappings().first()
        if not record or record["payload"].get("content_deleted"):
            raise NotFound("take_not_found")
        if write:
            assert_project_writable(conn, self.tenant, principal.owner, project_id)
        else:
            # Same content/history fences, without authoring or purge locks.
            _assert_project_available(
                conn, self.tenant, principal.owner, project_id, ("active",)
            )
        shot = shot_entity(record["payload"], shot_id)
        return migration_source_shots(
            conn,
            self.tenant,
            principal.owner,
            record["payload"],
            shot,
            record["version"],
        )

    def _candidates(self, principal, project_id, source_ids):
        return (
            select(
                artifacts.c.id,
                artifacts.c.metadata,
                artifacts.c.job_id,
                jobs.c.request["client_ref"]["shot_id"]
                .as_string()
                .label("source_shot"),
                jobs.c.result["artifact_ids"].label("accepted"),
            )
            .select_from(
                artifacts.join(jobs, artifacts.c.job_id == jobs.c.id).join(
                    attempts, artifacts.c.attempt_id == attempts.c.id
                )
            )
            .where(
                jobs.c.tenant_id == self.tenant,
                jobs.c.owner_id == principal.owner,
                jobs.c.project_id == project_id,
                jobs.c.request["client_ref"]["project_id"].as_string() == project_id,
                jobs.c.request["client_ref"]["shot_id"].as_string().in_(source_ids),
                jobs.c.status == "succeeded",
                attempts.c.status == "succeeded",
                attempts.c.job_id == jobs.c.id,
                attempts.c.id == jobs.c.current_attempt_id,
                artifacts.c.metadata["kind"].as_string() == "video",
            )
        )

    @staticmethod
    def _accepted(source):
        return (
            source
            and isinstance(source["accepted"], list)
            and source["id"] in source["accepted"]
            and not source["metadata"].get("content_deleted")
        )

    def _source(self, conn, principal, project_id, shot_id, artifact_id, *, write):
        source_ids = self._source_ids(conn, principal, project_id, shot_id, write=write)
        source = (
            conn.execute(
                self._candidates(principal, project_id, source_ids).where(
                    artifacts.c.id == artifact_id
                )
            )
            .mappings()
            .first()
        )
        if not self._accepted(source):
            raise NotFound("take_not_found")
        assert_content_available(
            conn, self.tenant, principal.owner, artifact_ids=(artifact_id,)
        )
        return source

    @contextmanager
    def _read_snapshot(self):
        guard = self.repo._memory_lock if self.repo._is_memory else nullcontext()
        with guard, self.repo.engine.connect() as conn:
            if conn.dialect.name == "postgresql":
                conn = conn.execution_options(
                    isolation_level="REPEATABLE READ", postgresql_readonly=True
                )
                conn.begin()
            else:
                # Deferred read snapshot, never SQLite's writer BEGIN IMMEDIATE.
                conn.exec_driver_sql("BEGIN")
            yield conn

    def get(self, principal, project_id, shot_id, artifact_id):
        with self._read_snapshot() as conn:
            self._source(conn, principal, project_id, shot_id, artifact_id, write=False)
            row = self._row(
                conn,
                Scope(self.tenant, principal.owner, project_id),
                "take_review",
                artifact_id,
            )
            if row and row["payload"].get("content_deleted"):
                raise NotFound("take_not_found")
            value = row["payload"] if row else {}
            return {
                "revision": row["version"] if row else 0,
                "verdict": value.get("verdict"),
                "marks": copy.deepcopy(value.get("marks", [])),
                "actor_kind": value.get("actor_kind"),
                "updated_at": row["updated_at"] if row else None,
            }

    def summaries(self, principal, project_id, shot_id):
        with self._read_snapshot() as conn:
            source_ids = self._source_ids(
                conn, principal, project_id, shot_id, write=False
            )
            candidates = list(
                conn.execute(
                    self._candidates(principal, project_id, source_ids)
                    .order_by(jobs.c.created_at.desc(), artifacts.c.id)
                    .limit(SUMMARY_LIMIT + 1)
                ).mappings()
            )
            if len(candidates) > SUMMARY_LIMIT:
                raise Conflict("take_review_summary_limit")
            live = []
            for candidate in candidates:
                if not self._accepted(candidate):
                    continue
                try:
                    assert_content_available(
                        conn,
                        self.tenant,
                        principal.owner,
                        artifact_ids=(candidate["id"],),
                    )
                except Conflict as error:
                    if str(error) not in {
                        "content_in_trash",
                        "content_purging_or_purged",
                    }:
                        raise
                    continue
                live.append(candidate["id"])
            if not live:
                return []
            rows = {
                row["document_id"]: row
                for row in conn.execute(
                    select(documents).where(
                        self.repo._scope(
                            documents, Scope(self.tenant, principal.owner, project_id)
                        ),
                        documents.c.kind == "take_review",
                        documents.c.document_id.in_(live),
                    )
                ).mappings()
            }
            result = []
            for ident in live:
                row = rows.get(ident)
                value = row["payload"] if row else {}
                if value.get("content_deleted"):
                    continue
                result.append(
                    {
                        "artifact_id": ident,
                        "revision": row["version"] if row else 0,
                        "verdict": value.get("verdict"),
                        "mark_count": len(value.get("marks", [])),
                        "actor_kind": value.get("actor_kind"),
                        "updated_at": row["updated_at"] if row else None,
                    }
                )
            return result

    def patch(self, principal, project_id, shot_id, artifact_id, body, key):
        from .quick_chat import key_check

        key_check(key)
        parsed = TakeReviewPatch.model_validate(body)
        body = parsed.model_dump(exclude_unset=True)
        scope = Scope(self.tenant, principal.owner, project_id)
        fingerprint = request_hash(
            {"shot_id": shot_id, "artifact_id": artifact_id, "body": body}
        )
        receipt_id = request_hash(
            {"actor": principal.actor_id, "artifact_id": artifact_id, "key": key}
        )
        with self.repo.transaction() as conn:
            source = self._source(
                conn, principal, project_id, shot_id, artifact_id, write=True
            )
            current = self._row(conn, scope, "take_review", artifact_id)
            if current and current["payload"].get("content_deleted"):
                raise NotFound("take_not_found")
            old = self._row(conn, scope, "take_review_receipt", receipt_id)
            if old:
                if old["payload"]["fingerprint"] != fingerprint:
                    raise Conflict("take_review_idempotency_conflict")
                return old["payload"]["response"]
            revision = current["version"] if current else 0
            if revision != parsed.expected_revision:
                raise Conflict("take_review_revision_conflict")
            previous = current["payload"] if current else {"verdict": None, "marks": []}
            value = {
                **copy.deepcopy(previous),
                **{k: v for k, v in body.items() if k in {"marks", "verdict"}},
            }
            if value["marks"]:
                duration = source["metadata"].get("duration_s")
                if (
                    type(duration) not in (int, float)
                    or not math.isfinite(duration)
                    or duration <= 0
                ):
                    raise Conflict("take_review_duration_unknown")
                if any(m["end_s"] > duration for m in value["marks"]):
                    raise ValueError("take_review_range_outside_duration")
            now = self.repo.clock()
            actor_kind = "agent" if principal.machine else "human"
            value.update(
                contract_version=1,
                artifact_id=artifact_id,
                source_job_id=source["job_id"],
                source_shot_id=source["source_shot"],
                actor_kind=actor_kind,
            )
            response = {
                "revision": revision + 1,
                "verdict": value["verdict"],
                "mark_count": len(value["marks"]),
                "actor_kind": actor_kind,
                "updated_at": now,
            }
            values = dict(payload=value, version=revision + 1, updated_at=now)
            if current:
                conn.execute(
                    update(documents)
                    .where(*self._where(scope, "take_review", artifact_id))
                    .values(**values)
                )
            else:
                conn.execute(
                    insert(documents).values(
                        tenant_id=self.tenant,
                        owner_id=principal.owner,
                        project_id=project_id,
                        kind="take_review",
                        document_id=artifact_id,
                        **values,
                    )
                )
            conn.execute(
                insert(documents).values(
                    tenant_id=self.tenant,
                    owner_id=principal.owner,
                    project_id=project_id,
                    kind="take_review_receipt",
                    document_id=receipt_id,
                    version=1,
                    updated_at=now,
                    payload={
                        "contract_version": 1,
                        "fingerprint": fingerprint,
                        "artifact_id": artifact_id,
                        "source_job_id": source["job_id"],
                        "source_shot_id": source["source_shot"],
                        "verdict_written": "verdict" in body,
                        "response": response,
                    },
                )
            )
        return response


def redact_reviews(conn, tenant, owner, *, artifact_ids=(), project_ids=(), now):
    """Called only inside the existing proven purge transaction, under owner gate."""
    if not artifact_ids and not project_ids:
        return
    conn.execute(
        update(documents)
        .where(
            documents.c.tenant_id == tenant,
            documents.c.owner_id == owner,
            documents.c.kind == "take_review",
            (documents.c.document_id.in_(artifact_ids))
            | (documents.c.project_id.in_(project_ids)),
        )
        .values(
            payload={"content_deleted": True, "marks": [], "verdict": None},
            version=documents.c.version + 1,
            updated_at=now,
        )
    )


def register_routes(app):
    service = TakeReviews(app.state.repository, app.state.settings.tenant_id)
    app.state.take_reviews = service

    @app.get(SUMMARY_PATH)
    def read_summaries(project_id: str, shot_id: str, request: Request):
        return JSONResponse(
            service.summaries(request.state.principal, project_id, shot_id),
            headers={"Cache-Control": "no-store"},
        )

    @app.get(PATH)
    def read_review(project_id: str, shot_id: str, artifact_id: str, request: Request):
        return JSONResponse(
            service.get(request.state.principal, project_id, shot_id, artifact_id),
            headers={"Cache-Control": "no-store"},
        )

    @app.patch(PATH)
    def patch_review(
        project_id: str,
        shot_id: str,
        artifact_id: str,
        body: TakeReviewPatch,
        request: Request,
        key: str = Header(..., alias="Idempotency-Key"),
    ):
        return JSONResponse(
            service.patch(
                request.state.principal,
                project_id,
                shot_id,
                artifact_id,
                body.model_dump(exclude_unset=True),
                key,
            ),
            headers={"Cache-Control": "no-store"},
        )
