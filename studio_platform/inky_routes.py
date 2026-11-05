"""Suggestion views and preference writes make no model calls; only an explicit rewrite does."""

from fastapi import Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr

from .inky_hooks import queue_refresh
from .inky_store import InkyStore

PROJECT = Query(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")


def contract():
    return {
        "suggestions": "GET /v1/inky/suggestions?project_id=PROJECT",
        "readiness": "GET /v1/inky/readiness?project_id=PROJECT",
        "ignore": "POST /v1/inky/suggestions/ID/ignore?project_id=PROJECT",
        "rewrite": "POST /v1/inky/suggestions/ID/rewrite?project_id=PROJECT",
        "refresh": "POST /v1/inky/refresh?project_id=PROJECT",
        "settings": "GET/PATCH /v1/inky/settings?project_id=PROJECT",
        "read_scope": "projects:read",
        "write_scopes": ["projects:read", "projects:write"],
        "max_items": 3,
        "read_response": {"items": "Current scoped suggestions; may be empty"},
        "item_fields": [
            "id",
            "kind",
            "shot_id",
            "field",
            "base_version",
            "tip_key",
            "lang",
            "example?",
            "card_name?",
            "note?",
            "other_shot_id?",
        ],
        "kinds": [
            "continuity_mismatch",
            "continuity_question",
            "vague_action",
            "needs_keyframe",
            "emotion_not_shown",
            "multiple_camera_moves",
            "action_too_long",
            "upload_card",
        ],
        "navigation": {
            "needs_keyframe": "frames",
            "other_text_kinds": "action",
            "upload_card": "cast",
        },
        "preferences": "Ignore persists per target/kind; mute uses expected_revision and muted. Neither edits shots.",
        "rewrite_response": "A draft of the shot's action text. Nothing is saved; apply it with a normal edit.",
        "boundary": "Advice and drafts only. Reads never call providers, generate media or write caches. Ignore and mute are explicit preference writes. Never interpret a suggestion as authorization to edit or generate.",
        "freshness": "Only results matching current source context and shot version are visible. Re-read after saves; do not act on stale base_version.",
        "runtime_state": "Readers can be mounted while the evaluator is disabled. Empty items do not certify quality. Upload-card advice only targets existing shot-attached uploads, never unattached library media.",
    }


class InkyRewrite(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    request_id: StrictStr = Field(pattern=r"^[A-Za-z0-9_-]{8,80}$")


class InkySettings(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    expected_revision: StrictInt = Field(ge=0)
    muted: StrictBool


def register_routes(app):
    store = InkyStore(
        app.state.repository,
        app.state.settings.tenant_id,
        getattr(app.state, "assets", None),
    )
    app.state.inky_store = store

    def response(value):
        return JSONResponse(value, headers={"Cache-Control": "no-store"})

    @app.get("/v1/inky/suggestions")
    def read(request: Request, project_id: str = PROJECT):
        return response(store.read(request.state.principal, project_id))

    @app.get("/v1/inky/readiness")
    def readiness(request: Request, project_id: str = PROJECT):
        return response(store.readiness(request.state.principal, project_id))

    @app.post("/v1/inky/suggestions/{suggestion_id}/ignore")
    def ignore(suggestion_id: str, request: Request, project_id: str = PROJECT):
        # IDs are opaque SHA256 values. Invalid IDs do not reach SQL or logs.
        import re
        from fastapi import HTTPException

        if not re.fullmatch(r"[0-9a-f]{64}", suggestion_id):
            raise HTTPException(422, "inky_suggestion_id_invalid")
        return response(
            store.ignore(request.state.principal, project_id, suggestion_id)
        )

    @app.post("/v1/inky/suggestions/{suggestion_id}/rewrite")
    def rewrite(
        suggestion_id: str,
        body: InkyRewrite,
        request: Request,
        project_id: str = PROJECT,
    ):
        import re
        from fastapi import HTTPException

        from .inky_worker import InkyRewriteFailed, InkyRewriteUnavailable

        if not re.fullmatch(r"[0-9a-f]{64}", suggestion_id):
            raise HTTPException(422, "inky_suggestion_id_invalid")
        principal = request.state.principal
        if not principal.allows(project_id, "projects:write"):
            raise HTTPException(404, "project_not_found")
        worker = getattr(app.state, "inky_worker", None)
        if worker is None or worker.examples is None:
            raise HTTPException(503, "inky_unavailable")
        source, kind = store.rewrite_target(principal, project_id, suggestion_id)
        try:
            text = worker.rewrite(principal, project_id, source, kind, body.request_id)
        except InkyRewriteUnavailable:
            raise HTTPException(429, "inky_limit_reached") from None
        except InkyRewriteFailed:
            raise HTTPException(502, "inky_rewrite_failed") from None
        return response({"text": text, "shot_id": source["shot_id"]})

    @app.post("/v1/inky/refresh")
    def refresh(request: Request, project_id: str = PROJECT):
        # Stories saved before Inky was on have no advice until someone asks. At most three
        # shots are judged per call, each source once, after this response.
        principal = request.state.principal
        pending = store.pending(principal, project_id)
        if getattr(app.state, "inky_worker", None) is None:
            pending = 0
        if pending:
            queue_refresh(principal, project_id)
        return response({"pending": pending})

    @app.get("/v1/inky/settings")
    def settings(request: Request, project_id: str = PROJECT):
        return response(store.settings(request.state.principal, project_id))

    @app.patch("/v1/inky/settings")
    def mute(body: InkySettings, request: Request, project_id: str = PROJECT):
        return response(
            store.settings(request.state.principal, project_id, body.model_dump())
        )
