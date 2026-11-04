"""Voices and generated lines (#997). Default OFF; no silent fallback; no vendor names out.

A generated line is a normal audio asset on the story, with what made it kept beside it.
Charging is not built yet: with customer charging on, a line is refused unless the
staging-only test switch is on, so nothing can be given away by accident.
"""

import hashlib
import io
import re

from fastapi import HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, StrictStr

from .repository import Conflict
from .speech_catalog import VOICES, public
from .speech_provider import DIRECTIONS, SpeechError, configured, level

REQUEST_ID = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
MAX_TEXT = 500


class SpeechBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    request_id: StrictStr = Field(min_length=8, max_length=64)
    text: StrictStr = Field(min_length=1, max_length=MAX_TEXT)
    voice_id: StrictStr = Field(min_length=1, max_length=40)
    direction: StrictStr = "calm"
    language: StrictStr = "zh"


def _fail(code, status, **detail):
    raise HTTPException(status, {"code": code, **detail})


def clean(text):
    """Direction is ours: a line's own brackets must not steer the delivery."""
    spoken = re.sub(r"[\[\]\x00-\x1f]", " ", text)
    return re.sub(r" {2,}", " ", spoken).strip()


def checked(body):
    """The catalog voice and the words to speak, or a refusal before anything is spent."""
    voice = VOICES.get(body.voice_id)
    text = clean(body.text)
    if voice is None:
        _fail("voice_unknown", 422)
    if (
        not REQUEST_ID.fullmatch(body.request_id)
        or body.direction not in DIRECTIONS
        or body.language not in ("zh", "en")
        or not text
    ):
        _fail("speech_request_invalid", 422)
    return voice, text


def answer(entity):
    return {
        "entity_id": entity["id"],
        "asset_id": entity["data"]["cloudAssetId"],
        "content_path": entity["data"]["cloudContentPath"],
        "voice_id": entity["data"]["speech"]["voice_id"],
    }


def find(project, entity_id):
    return next((e for e in project.get("entities", []) if e["id"] == entity_id), None)


def made_by(body, voice, text, characters):
    return {
        "voice_id": body.voice_id,
        "provider": voice["provider"],
        "model": voice["model"],
        "provider_voice": voice["provider_voice"],
        "direction": body.direction,
        "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
        "characters": characters,
    }


def attach(app, principal, project_id, entity_id, asset_id, title, provenance, key):
    """Put the stored clip on the story; a concurrent edit only means trying again."""
    for _attempt in range(3):
        row = app.state.authorized_project(principal, project_id, "projects:write")
        try:
            app.state.guided.mutate(
                principal,
                project_id,
                {
                    "expected_version": row["version"],
                    "actions": [
                        {
                            "op": "asset.attach",
                            "asset_id": asset_id,
                            "entity_id": entity_id,
                            "title": title,
                        },
                        {
                            "op": "entity.update",
                            "entity_id": entity_id,
                            "patch": {"data": {"speech": provenance}},
                        },
                    ],
                },
                key,
            )
        except Conflict as error:
            if str(error) != "document_version_conflict":
                _fail("speech_store_failed", 502, nothing_charged=True)
            continue
        current = app.state.authorized_project(principal, project_id, "projects:read")
        return answer(find(current["payload"], entity_id))
    _fail("document_version_conflict", 409, nothing_charged=True)


def register_routes(app):
    settings = app.state.settings
    app.state.speech_adapters = None

    def adapters():
        if app.state.speech_adapters is None:
            app.state.speech_adapters = configured(settings) or {}
        return app.state.speech_adapters

    def switches():
        from .speech_settings import read

        with app.state.repository.engine.connect() as connection:
            return read(connection, settings.tenant_id)

    def charging():
        from . import credits

        with app.state.repository.engine.connect() as connection:
            policy = credits.credit_policy(connection, settings.tenant_id)
        return policy.get("enabled") is True

    def require_admin(request):
        from .identity import is_admin

        principal = request.state.principal
        if not is_admin(principal, app.state.repository, settings):
            _fail("admin_required", 403)
        return principal

    def allowed():
        current = switches()
        if not current["voice_enabled"]:
            _fail("voice_disabled", 409, nothing_charged=True)
        if charging() and not current["free_while_testing"]:
            _fail("speech_billing_unavailable", 503, nothing_charged=True)

    def store(principal, project_id, data, request_id):
        asset = app.state.assets.upload(
            principal.owner,
            project_id,
            io.BytesIO(data),
            "line.mp3",
            client_asset_id="speech:" + request_id,
        )
        if asset.get("status") != "ready":
            _fail("speech_store_failed", 502, nothing_charged=True)
        return asset["asset_id"]

    @app.get("/v1/voices")
    def voices():
        enabled = switches()["voice_enabled"] and bool(adapters())
        return {"enabled": enabled, "voices": public() if enabled else []}

    @app.post("/v1/projects/{project_id}/speech")
    def speak(project_id: str, body: SpeechBody, request: Request):
        from .generation_admission import reject_managed

        principal = request.state.principal
        row = app.state.authorized_project(principal, project_id, "projects:write")
        reject_managed(row["payload"])
        allowed()
        voice, text = checked(body)
        entity_id = "speech-" + body.request_id
        replay = find(row["payload"], entity_id)
        if replay is not None:
            return answer(replay)
        adapter = adapters().get(voice["provider"])
        if adapter is None:
            _fail("assistant_unavailable", 503, nothing_charged=True)
        try:
            spoken = adapter.synthesize(text, voice, body.direction, body.language)
            data = level(spoken.data)
        except SpeechError as error:
            _fail(error.code, error.status, nothing_charged=True)
        return attach(
            app,
            principal,
            project_id,
            entity_id,
            store(principal, project_id, data, body.request_id),
            text[:40],
            made_by(body, voice, text, spoken.characters),
            "speech-attach-" + body.request_id,
        )

    @app.get("/v1/admin/speech/settings")
    def get_settings(request: Request):
        require_admin(request)
        return switches()

    @app.put("/v1/admin/speech/settings")
    def put_settings(request: Request, body: dict):
        from .speech_settings import SpeechSettingsError, write

        principal = require_admin(request)
        repo = app.state.repository
        try:
            with repo.transaction() as connection:
                return write(
                    connection,
                    settings.tenant_id,
                    principal,
                    body,
                    repo.clock(),
                    deployment_target=settings.deployment_target,
                )
        except SpeechSettingsError as error:
            _fail(error.code, error.status)
