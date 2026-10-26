"""Scoped product feedback, opt-out and one-shot existing Sentry forwarding."""

import re
import math
import uuid
from typing import Literal

from fastapi import Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    model_validator,
)
from sqlalchemy import func, insert, select, update

from .content_lifecycle import owner_reference_gate, _assert_project_available
from .generation_draft import shot_entity
from .repository import Conflict, NotFound, Scope, documents, request_hash
from .take_reviews import TakeReviews

CATEGORIES = (
    "tool_error",
    "missing_capability",
    "confusing_output",
    "documentation",
    "other",
    "model_limit",
)
OWNER_HOURLY = 30
ACTOR_HOURLY = 10
PRIVATE_TEXT = re.compile(
    r"https?://|mailto:|[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}|"
    r"\bBearer\s+\S+|\bsk-[A-Za-z0-9_-]{12,}|\bAKIA[A-Z0-9]{16}|"
    r"-----BEGIN|\b(?:password|api[_-]?key|access[_-]?token|cookie)\s*[:=]",
    re.I,
)


class FeedbackBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    category: Literal[
        "tool_error",
        "missing_capability",
        "confusing_output",
        "documentation",
        "other",
        "model_limit",
    ]
    severity: Literal["low", "medium", "high"]
    summary: StrictStr = Field(min_length=1, max_length=280)
    suggestion: StrictStr = Field(default="", max_length=500)
    source: Literal["story_chat", "agent", "telegram", "system"]
    project_id: StrictStr | None = Field(
        default=None, min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_-]+$"
    )
    shot_id: StrictStr | None = Field(
        default=None, min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_-]+$"
    )
    artifact_id: StrictStr | None = Field(
        default=None, min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_-]+$"
    )
    intent: StrictStr = Field(default="", max_length=200)
    include_context: StrictBool = False

    @model_validator(mode="after")
    def privacy(self):
        if not self.summary.strip():
            raise ValueError("feedback_summary_required")
        for value in (self.summary, self.suggestion, self.intent):
            if PRIVATE_TEXT.search(value) or any(ord(c) < 32 for c in value):
                raise ValueError("feedback_private_text_refused")
        if (self.shot_id and not self.project_id) or (
            self.artifact_id and not self.shot_id
        ):
            raise ValueError("feedback_reference_incomplete")
        return self


class FeedbackSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    expected_revision: StrictInt = Field(ge=0)
    enabled: StrictBool


def contract():
    return {
        "path": "/v1/feedback",
        "post": "Idempotency-Key required. Browser or PAT projects:read; references require matching project access. category,severity,summary<=280,source; optional suggestion<=500,intent<=200,project_id,shot_id,artifact_id. Artifact requires shot and project; no arbitrary IDs.",
        "categories": list(CATEGORIES),
        "settings": "Human GET/PATCH /v1/feedback/settings; {expected_revision,enabled} plus saved key. Default enabled:true; context inclusion stays disabled. Agents cannot opt in for the owner.",
        "recovery": "GET /v1/feedback/{id}; same POST key/body returns saved receipt without resending. forwarding pending/enqueued/unknown/disabled/suppressed is not remote delivery proof. Do not retry unknown with a new key.",
        "read": "GET /v1/feedback?category=&source=&limit=1..100; own account only, PAT only its own reports and currently allowed projects. No cross-owner admin view.",
        "privacy": "No automatic prompts/media/credentials, cookies, URLs, email, attachments or request scopes. Reject obvious sensitive text; callers must summarize product behavior without customer content. include_context:true refused. References are pseudonymous at Sentry. Local intent is metadata only, not generation authority.",
        "limits": {
            "account_per_hour": OWNER_HOURLY,
            "connection_per_hour": ACTOR_HOURLY,
        },
        "discovery": "Report an unexpected tool limitation or confusing output here once with a saved key; never auto-post to public GitHub. This slice adds no automatic Story Chat/system reporting, global write intent hook or LLM tracing.",
    }


def forward_feedback(client, report):
    """Closed modern feedback envelope; never inherits SDK/global request scopes.

    Python's pinned SDK has no capture_feedback. Reuse its configured transport,
    not the deprecated user_report API or the exception before_send path.
    The enqueue result is deliberately NOT represented as Sentry delivery proof.
    """
    from .error_monitoring import DSN
    from sentry_sdk.envelope import Envelope, Item, PayloadRef

    if (
        not client
        or client.dsn != DSN
        or not client.transport
        or client.options.get("environment") not in {"staging", "production"}
    ):
        return "disabled"
    body = FeedbackBody.model_validate(report["body"])
    if body.include_context:
        return "suppressed"
    event_id = uuid.UUID(report["event_id"]).hex
    if (
        report["actor_kind"] not in {"human", "agent"}
        or not re.fullmatch(r"[0-9a-f]{64}", report["report_id"])
        or type(report["created_at"]) not in (int, float)
        or not math.isfinite(report["created_at"])
    ):
        raise ValueError("feedback_envelope_invalid")
    refs = {
        name: value
        for name, value in report["references"].items()
        if name in {"owner_ref", "project_ref", "shot_ref", "artifact_ref"}
        and isinstance(value, str)
        and re.fullmatch(r"[0-9a-f]{64}", value)
    }
    payload = {
        "event_id": event_id,
        "timestamp": report["created_at"],
        "platform": "python",
        "environment": client.options["environment"],
        "contexts": {"feedback": {"message": body.summary}},
        "tags": {
            "category": body.category,
            "severity": body.severity,
            "source": body.source,
            "actor_kind": report["actor_kind"],
            "report_id": report["report_id"],
            **refs,
        },
    }
    envelope = Envelope(headers={"event_id": event_id})
    envelope.add_item(Item(type="feedback", payload=PayloadRef(json=payload)))
    client.transport.capture_envelope(envelope)
    return "enqueued"


class ProductFeedback:
    def __init__(self, app):
        self.app, self.repo = app, app.state.repository
        self.tenant = app.state.settings.tenant_id

    def scope(self, principal):
        return Scope(self.tenant, principal.owner, "__feedback")

    def where(self, principal, kind, ident=None):
        return (
            self.repo._scope(documents, self.scope(principal)),
            documents.c.kind == kind,
            *(() if ident is None else (documents.c.document_id == ident,)),
        )

    def row(self, conn, principal, kind, ident):
        return (
            conn.execute(select(documents).where(*self.where(principal, kind, ident)))
            .mappings()
            .first()
        )

    def put(self, conn, principal, kind, ident, payload, *, version=1, exists=False):
        values = dict(payload=payload, version=version, updated_at=self.repo.clock())
        if exists:
            conn.execute(
                update(documents)
                .where(*self.where(principal, kind, ident))
                .values(**values)
            )
        else:
            conn.execute(
                insert(documents).values(
                    tenant_id=self.tenant,
                    owner_id=principal.owner,
                    project_id="__feedback",
                    kind=kind,
                    document_id=ident,
                    **values,
                )
            )

    @staticmethod
    def authorize(principal, *, human=False):
        if principal.support_viewer or (
            principal.machine and (human or "projects:read" not in principal.scopes)
        ):
            raise HTTPException(403, "feedback_forbidden")

    def references(self, conn, principal, body):
        if body.project_id:
            if not principal.allows(body.project_id, "projects:read"):
                raise NotFound("project_not_found")
            row = (
                conn.execute(
                    select(documents).where(
                        self.repo._scope(
                            documents, Scope(self.tenant, principal.owner, "__projects")
                        ),
                        documents.c.kind == "project",
                        documents.c.document_id == body.project_id,
                    )
                )
                .mappings()
                .first()
            )
            if not row or row["payload"].get("content_deleted"):
                raise NotFound("project_not_found")
            _assert_project_available(
                conn, self.tenant, principal.owner, body.project_id, ("active",)
            )
            if body.shot_id:
                shot_entity(row["payload"], body.shot_id)
            if body.artifact_id:
                TakeReviews(self.repo, self.tenant)._source(
                    conn,
                    principal,
                    body.project_id,
                    body.shot_id,
                    body.artifact_id,
                    write=False,
                )

    def settings_value(self, conn, principal):
        row = self.row(conn, principal, "feedback_settings", "preferences")
        return {
            "revision": row["version"] if row else 0,
            "enabled": row["payload"].get("enabled", True) if row else True,
            "include_context_allowed": False,
        }

    def settings(self, principal):
        self.authorize(principal, human=True)
        with self.repo.engine.connect() as conn:
            return self.settings_value(conn, principal)

    def configure(self, principal, body, key):
        from .quick_chat import key_check

        self.authorize(principal, human=True)
        key_check(key)
        parsed = FeedbackSettings.model_validate(body)
        body = parsed.model_dump()
        ident = request_hash({"actor": principal.actor_id, "key": key})
        with self.repo.transaction() as conn:
            owner_reference_gate(conn, self.tenant, principal.owner)
            old = self.row(conn, principal, "feedback_settings_receipt", ident)
            if old:
                if old["payload"]["fingerprint"] != request_hash(body):
                    raise Conflict("feedback_idempotency_conflict")
                return old["payload"]["response"]
            current = self.settings_value(conn, principal)
            if current["revision"] != parsed.expected_revision:
                raise Conflict("feedback_settings_revision_conflict")
            response = {
                **current,
                "enabled": parsed.enabled,
                "revision": current["revision"] + 1,
            }
            self.put(
                conn,
                principal,
                "feedback_settings",
                "preferences",
                {"enabled": parsed.enabled},
                version=response["revision"],
                exists=current["revision"] > 0,
            )
            self.put(
                conn,
                principal,
                "feedback_settings_receipt",
                ident,
                {"fingerprint": request_hash(body), "response": response},
            )
        return response

    def visible(self, principal, value):
        project = value.get("body", {}).get("project_id")
        return not value.get("content_deleted") and (
            not principal.machine
            or (
                value.get("actor_hash") == request_hash(principal.actor_id)
                and (not project or principal.allows(project, "projects:read"))
            )
        )

    def get(self, principal, ident):
        self.authorize(principal)
        with self.repo.engine.connect() as conn:
            row = self.row(conn, principal, "product_feedback", ident)
            if not row or not self.visible(principal, row["payload"]):
                raise NotFound("feedback_not_found")
            return row["payload"]["response"]

    def list(self, principal, category=None, source=None, limit=50):
        self.authorize(principal)
        if (
            type(limit) is not int
            or not 1 <= limit <= 100
            or (category and category not in CATEGORIES)
            or (source and source not in {"story_chat", "agent", "telegram", "system"})
        ):
            raise ValueError("feedback_filter_invalid")
        query = select(documents).where(*self.where(principal, "product_feedback"))
        if principal.machine:
            query = query.where(
                documents.c.payload["actor_hash"].as_string()
                == request_hash(principal.actor_id)
            )
        if category:
            query = query.where(
                documents.c.payload["body"]["category"].as_string() == category
            )
        if source:
            query = query.where(
                documents.c.payload["body"]["source"].as_string() == source
            )
        with self.repo.engine.connect() as conn:
            rows = (
                conn.execute(
                    query.order_by(
                        documents.c.updated_at.desc(), documents.c.document_id
                    ).limit(limit + 1)
                )
                .mappings()
                .all()
            )
        return {
            "reports": [
                r["payload"]["response"]
                for r in rows[:limit]
                if self.visible(principal, r["payload"])
            ],
            "has_more": len(rows) > limit,
        }

    def post(self, principal, body, key):
        from .quick_chat import key_check

        self.authorize(principal)
        key_check(key)
        parsed = FeedbackBody.model_validate(body)
        if parsed.include_context:
            raise HTTPException(403, "feedback_context_disabled")
        body = parsed.model_dump()
        ident = request_hash({"actor": principal.actor_id, "key": key})
        fingerprint = request_hash(body)
        with self.repo.transaction() as conn:
            owner_reference_gate(conn, self.tenant, principal.owner)
            self.references(conn, principal, parsed)
            old = self.row(conn, principal, "product_feedback", ident)
            if old:
                if old["payload"].get("content_deleted"):
                    raise NotFound("feedback_not_found")
                if old["payload"]["fingerprint"] != fingerprint:
                    raise Conflict("feedback_idempotency_conflict")
                return old["payload"]["response"]
            if not self.settings_value(conn, principal)["enabled"]:
                raise HTTPException(403, "feedback_disabled")
            now, actor = self.repo.clock(), request_hash(principal.actor_id)
            recent = select(func.count()).where(
                *self.where(principal, "product_feedback"),
                documents.c.payload["created_at"].as_float() >= now - 3600,
            )
            if (
                conn.execute(recent).scalar_one() >= OWNER_HOURLY
                or conn.execute(
                    recent.where(documents.c.payload["actor_hash"].as_string() == actor)
                ).scalar_one()
                >= ACTOR_HOURLY
            ):
                raise HTTPException(
                    429, "feedback_rate_limit", headers={"Retry-After": "3600"}
                )
            event_id = uuid.uuid4().hex
            response = {
                "report_id": ident,
                "event_id": event_id,
                "created_at": now,
                "category": parsed.category,
                "severity": parsed.severity,
                "source": parsed.source,
                "forwarding": "pending",
            }
            report = {
                "report_id": ident,
                "event_id": event_id,
                "created_at": now,
                "body": body,
                "actor_hash": actor,
                "actor_kind": "agent" if principal.machine else "human",
                "fingerprint": fingerprint,
                "response": response,
                "references": {
                    "owner_ref": request_hash(
                        {"tenant": self.tenant, "owner": principal.owner}
                    ),
                    **{
                        name + "_ref": request_hash(
                            {
                                "tenant": self.tenant,
                                "owner": principal.owner,
                                "id": ident,
                            }
                        )
                        for name, ident in (
                            ("project", parsed.project_id),
                            ("shot", parsed.shot_id),
                            ("artifact", parsed.artifact_id),
                        )
                        if ident
                    },
                },
            }
            self.put(conn, principal, "product_feedback", ident, report)
        # Only this original invocation reaches forwarding. A replay returns above.
        # Persist pending first, then serialize opt-out/deletion with the one enqueue.
        with self.repo.transaction() as conn:
            owner_reference_gate(conn, self.tenant, principal.owner)
            self.references(conn, principal, parsed)
            if not self.settings_value(conn, principal)["enabled"]:
                status = "suppressed"
            else:
                try:
                    status = forward_feedback(
                        getattr(self.app.state, "error_monitoring_client", None), report
                    )
                except Exception:
                    status = "unknown"
            report["response"] = {**response, "forwarding": status}
            self.put(
                conn,
                principal,
                "product_feedback",
                ident,
                report,
                version=2,
                exists=True,
            )
        return report["response"]


def redact_feedback(conn, tenant, owner, *, artifact_ids=(), project_ids=(), now):
    if not artifact_ids and not project_ids:
        return
    conn.execute(
        update(documents)
        .where(
            documents.c.tenant_id == tenant,
            documents.c.owner_id == owner,
            documents.c.kind == "product_feedback",
            (documents.c.payload["body"]["artifact_id"].as_string().in_(artifact_ids))
            | (documents.c.payload["body"]["project_id"].as_string().in_(project_ids)),
        )
        .values(
            payload={"content_deleted": True},
            version=documents.c.version + 1,
            updated_at=now,
        )
    )


def register_routes(app):
    service = ProductFeedback(app)
    app.state.product_feedback = service

    def response(value):
        return JSONResponse(value, headers={"Cache-Control": "no-store"})

    @app.get("/v1/feedback/settings")
    def settings(request: Request):
        return response(service.settings(request.state.principal))

    @app.patch("/v1/feedback/settings")
    def configure(
        body: FeedbackSettings,
        request: Request,
        key: str = Header(..., alias="Idempotency-Key"),
    ):
        return response(
            service.configure(request.state.principal, body.model_dump(), key)
        )

    @app.post("/v1/feedback")
    def post(
        body: FeedbackBody,
        request: Request,
        key: str = Header(..., alias="Idempotency-Key"),
    ):
        return response(service.post(request.state.principal, body.model_dump(), key))

    @app.get("/v1/feedback")
    def listing(
        request: Request,
        category: str | None = None,
        source: str | None = None,
        limit: int = 50,
    ):
        return response(service.list(request.state.principal, category, source, limit))

    @app.get("/v1/feedback/{report_id}")
    def read(report_id: str, request: Request):
        return response(service.get(request.state.principal, report_id))
