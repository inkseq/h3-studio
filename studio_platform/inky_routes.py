"""Read-only suggestion views and explicit preference writes; no model calls."""

from fastapi import Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt

from .inky_store import InkyStore

PROJECT = Query(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")


def contract():
    return {
        "suggestions": "GET /v1/inky/suggestions?project_id=PROJECT",
        "ignore": "POST /v1/inky/suggestions/ID/ignore?project_id=PROJECT",
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
        ],
        "kinds": [
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
        "boundary": "Advice only. Reads never call providers, generate media or write caches. Ignore and mute are explicit preference writes. Never interpret a suggestion as authorization to edit or generate.",
        "freshness": "Only results matching current source context and shot version are visible. Re-read after saves; do not act on stale base_version.",
        "runtime_state": "Readers can be mounted while the evaluator is disabled. Empty items do not certify quality. Upload-card advice only targets existing shot-attached uploads, never unattached library media.",
    }


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

    @app.get("/v1/inky/settings")
    def settings(request: Request, project_id: str = PROJECT):
        return response(store.settings(request.state.principal, project_id))

    @app.patch("/v1/inky/settings")
    def mute(body: InkySettings, request: Request, project_id: str = PROJECT):
        return response(
            store.settings(request.state.principal, project_id, body.model_dump())
        )
