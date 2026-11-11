"""Design a character's voice from words; keep one preview as its voice sample.

Free while new (owner, 2026-11-07). The voice switch must be on, and a per-account
daily cap bounds the provider bill. Previews are unattached audio assets; keeping one
attaches it to the story like any other voice clip, and the client then adds it to the
character exactly like a "speak this line" clip.
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import io
import threading

from fastapi import HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr

from .repository import Conflict, NotFound
from .speech_provider import level
from .speech_routes import require_paid_work
from .voice_design import (
    MAX_DESCRIPTION,
    MAX_SAMPLE,
    VoiceDesignError,
    brief,
    configured,
)

PREVIEWS = 3
DAILY_LIMIT = 30  # designs per account per UTC day while design is free


class DesignBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    request_id: StrictStr = Field(pattern=r"^[A-Za-z0-9_-]{8,64}$")
    character_id: StrictStr = Field(min_length=1, max_length=160)
    language: StrictStr = Field(pattern=r"^(zh|en)$")
    speaker: StrictStr = Field(default="", max_length=80)
    description: StrictStr = Field(default="", max_length=MAX_DESCRIPTION)
    sample: StrictStr = Field(default="", max_length=MAX_SAMPLE)


class KeepBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    request_id: StrictStr = Field(pattern=r"^[A-Za-z0-9_-]{8,64}$")
    index: StrictInt = Field(ge=0, lt=PREVIEWS)
    asset_id: StrictStr = Field(min_length=1, max_length=160)
    title: StrictStr = Field(min_length=1, max_length=80)


def _fail(code, status, **detail):
    raise HTTPException(status, {"code": code, **detail})


def _character(project, ident):
    entity = next((e for e in project["entities"] if e["id"] == ident), None)
    if entity is None or entity.get("type") != "character":
        _fail("voice_design_character_missing", 404)
    return entity


def _collect(futures, store):
    """Stored previews; whether any was refused by the provider or failed."""
    previews, refused, failed = [], False, False
    for index, future in enumerate(futures):
        try:
            previews.append({"index": index, "asset_id": store(index, future.result())})
        except VoiceDesignError as error:
            refused = refused or error.code == "voice_design_refused"
            failed = True
        except Exception:
            failed = True
    return previews, refused, failed


def register_routes(app):
    settings = app.state.settings
    app.state.voice_designer = None
    counts, lock = {}, threading.Lock()

    def designer():
        if app.state.voice_designer is None:
            app.state.voice_designer = configured(settings) or False
        return app.state.voice_designer or None

    def voice_on():
        from .speech_settings import read

        with app.state.repository.engine.connect() as connection:
            if not read(connection, settings.tenant_id)["voice_enabled"]:
                _fail("voice_disabled", 409)

    def take_slot(owner):
        day = datetime.now(timezone.utc).date().isoformat()
        with lock:
            for key in [k for k in counts if k[1] != day]:
                del counts[key]
            used = counts.get((owner, day), 0)
            if used >= DAILY_LIMIT:
                _fail("voice_design_daily_limit", 429, limit=DAILY_LIMIT)
            counts[(owner, day)] = used + 1

    def chat_client():
        from .story_chat_routes import configured_client

        try:
            return configured_client(settings)
        except Exception:
            return None

    @app.post("/v1/projects/{project_id}/voice-designs")
    def design(project_id: str, body: DesignBody, request: Request):
        from .generation_admission import reject_managed

        principal = request.state.principal
        row = app.state.authorized_project(principal, project_id, "projects:write")
        require_paid_work(app, principal, project_id)
        reject_managed(row["payload"])
        card = _character(row["payload"], body.character_id)
        voice_on()
        adapter = designer()
        if adapter is None:
            _fail("assistant_unavailable", 503)
        take_slot(principal.owner)
        description = " ".join(body.description.split())
        sample = " ".join(body.sample.split())
        if not description or not sample:
            drafted = brief(chat_client(), card, body.speaker, body.language)
            description = description or drafted["description"]
            sample = sample or drafted["sample"]

        def store(index, data):
            asset = app.state.assets.upload(
                principal.owner,
                project_id,
                io.BytesIO(data),
                f"voice-{index + 1}.mp3",
                client_asset_id=f"voice-design:{body.request_id}:{index}",
            )
            if asset.get("status") != "ready":
                raise VoiceDesignError("voice_design_store_failed")
            return asset["asset_id"]

        # The provider calls run side by side; the story's files are written one at a time.
        with ThreadPoolExecutor(PREVIEWS) as pool:
            futures = [
                pool.submit(
                    lambda: level(adapter.design(description, sample, body.language))
                )
                for _ in range(PREVIEWS)
            ]
        previews, refused, failed = _collect(futures, store)
        if not previews:
            _fail(
                "voice_design_refused" if refused else "voice_design_failed",
                422 if refused else 502,
            )
        return {
            "request_id": body.request_id,
            "description": description,
            "sample": sample,
            "language": body.language,
            "previews": previews,
            "partial": failed,
        }

    @app.post("/v1/projects/{project_id}/voice-designs/keep")
    def keep(project_id: str, body: KeepBody, request: Request):
        principal = request.state.principal
        try:
            asset = app.state.assets.get(principal.owner, body.asset_id)
        except Exception:
            _fail("voice_design_preview_missing", 404)
        if (
            asset.get("project_id") != project_id
            or asset.get("client_asset_id")
            != f"voice-design:{body.request_id}:{body.index}"
        ):
            _fail("voice_design_preview_missing", 404)
        entity_id = f"voice-design-{body.request_id}-{body.index}"
        for _ in range(3):
            row = app.state.authorized_project(principal, project_id, "projects:write")
            if any(e["id"] == entity_id for e in row["payload"]["entities"]):
                return {
                    "entity_id": entity_id
                }  # already kept: same clip, no second one
            try:
                app.state.guided.mutate(
                    principal,
                    project_id,
                    {
                        "expected_version": row["version"],
                        "actions": [
                            {
                                "op": "asset.attach",
                                "asset_id": body.asset_id,
                                "entity_id": entity_id,
                                "title": body.title,
                            }
                        ],
                    },
                    f"voice-design-keep-{body.request_id}-{body.index}",
                )
            except Conflict as error:
                if str(error) == "document_version_conflict":
                    continue
                _fail("voice_design_keep_failed", 502)
            except (ValueError, NotFound):
                _fail("voice_design_keep_failed", 422)
            return {"entity_id": entity_id}
        _fail("document_version_conflict", 409)
