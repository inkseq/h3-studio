"""Read-only suggestion views and explicit preference writes; no model calls."""

from fastapi import Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt

from .inky_store import InkyStore

PROJECT = Query(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")


class InkySettings(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    expected_revision: StrictInt = Field(ge=0)
    muted: StrictBool


def register_routes(app):
    store = InkyStore(app.state.repository, app.state.settings.tenant_id)
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
