"""Voices and generated lines (#997). Default OFF; no silent fallback; no vendor names out.

A generated line is a normal audio asset on the story, with what made it kept beside it.
The existing credit ledger reserves a frozen quote and captures only after the
original audio attachment succeeds. Production switches remain unchanged.
"""

import io
import json
from pathlib import Path
import re

from fastapi import HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field, StrictStr

from .credits import CreditError
from .repository import Conflict
from .speech_credit_proof import quote as speech_quote
from .speech_requests import SpeechRequest
from .speech_catalog import LANGUAGES, VOICES, public
from .speech_provider import DIRECTIONS, SpeechError, configured, level

REQUEST_ID = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
MAX_TEXT = 500
SAMPLES = Path(__file__).parent / "voice_samples"


class SpeechBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    request_id: StrictStr = Field(min_length=8, max_length=64)
    text: StrictStr = Field(min_length=1, max_length=MAX_TEXT)
    voice_id: StrictStr = Field(min_length=1, max_length=40)
    direction: StrictStr = "calm"
    language: StrictStr = "zh"


def _fail(code, status, **detail):
    raise HTTPException(status, {"code": code, **detail})


def require_paid_work(app, principal, project_id):
    """Voice costs credits or provider money, so it asks what a generation job asks."""
    if principal.machine:
        from .agent_management import connection_for_actor

        with app.state.repository.engine.connect() as connection:
            grant = connection_for_actor(
                connection,
                app.state.settings.tenant_id,
                principal.owner,
                principal.actor_id,
            )
        if (
            grant is not None
            and json.loads(grant["authorization"]).get("permission") == "organise"
        ):
            raise Conflict("connection_permission_denied")
    app.state.generation_access.authorize(principal, project_id, "jobs:write")


def clean(text):
    """Direction is ours: a line's own brackets must not steer the delivery."""
    spoken = re.sub(r"[\[\]\x00-\x1f]", " ", text)
    return re.sub(r" {2,}", " ", spoken).strip()


def checked(body, *, voice_required=True):
    """The catalog voice and the words to speak, or a refusal before anything is spent."""
    voice = VOICES.get(body.voice_id)
    text = clean(body.text)
    if voice is None and voice_required:
        _fail("voice_unknown", 422)
    if (
        not REQUEST_ID.fullmatch(body.request_id)
        or body.direction not in DIRECTIONS
        or body.language not in ("zh", "en")
        or not text
    ):
        _fail("speech_request_invalid", 422)
    return voice, text


def generate(attempt, adapter, voice, body, text, store):
    try:
        spoken = adapter.synthesize(text, voice, body.direction, body.language)
        data = level(spoken.data)
    except SpeechError as error:
        released = attempt.release("provider_failed")
        _fail(
            error.code, error.status, **({"nothing_charged": True} if released else {})
        )
    except Exception:
        released = attempt.release("provider_failed")
        _fail(
            "speech_provider_failed",
            502,
            **({"nothing_charged": True} if released else {}),
        )
    try:
        asset_id = store(attempt.principal, attempt.project_id, data, body.request_id)
    except Exception:
        released = attempt.release("store_failed")
        _fail(
            "speech_store_failed",
            502,
            **({"nothing_charged": True} if released else {}),
        )
    return attempt.attach(asset_id, text[:40], attempt.provenance(spoken.characters))


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
        return current

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

    @app.get("/v1/voices/{voice_id}/sample/{language}")
    def sample(voice_id: str, language: str):
        # Both parts come from closed lists, so a request never names a path.
        if voice_id not in VOICES or language not in LANGUAGES:
            _fail("voice_unknown", 404)
        return FileResponse(
            SAMPLES / f"{voice_id}-{language}.mp3",
            media_type="audio/mpeg",
            headers={"Cache-Control": "private, max-age=86400"},
        )

    @app.post("/v1/projects/{project_id}/speech/quote")
    def quote(project_id: str, body: SpeechBody, request: Request):
        from .generation_admission import reject_managed

        row = app.state.authorized_project(
            request.state.principal, project_id, "projects:write"
        )
        reject_managed(row["payload"])
        current = allowed()
        _voice, text = checked(body)
        return {
            "characters": len(text),
            **speech_quote(len(text)),
            "charging": charging() and not current["free_while_testing"],
        }

    @app.post("/v1/projects/{project_id}/speech")
    def speak(project_id: str, body: SpeechBody, request: Request):
        from .generation_admission import reject_managed

        principal = request.state.principal
        row = app.state.authorized_project(principal, project_id, "projects:write")
        require_paid_work(app, principal, project_id)
        reject_managed(row["payload"])
        # Existing receipts retain their catalog and charging decision. Catalog or
        # enable-switch changes only decide admission of a genuinely new request.
        voice, text = checked(body, voice_required=False)
        attempt = SpeechRequest(app, principal, project_id, body, text)
        try:
            replay = attempt.replay()
            if replay is not None:
                return replay
            current = allowed()
            if voice is None:
                _fail("voice_unknown", 422, nothing_charged=True)
            adapter = adapters().get(voice["provider"])
            if adapter is None:
                _fail("assistant_unavailable", 503, nothing_charged=True)
            attempt.begin(
                voice, charged=charging() and not current["free_while_testing"]
            )
            return generate(attempt, adapter, voice, body, text, store)
        except CreditError as error:
            _fail(error.code, error.status, **error.data)
        except HTTPException:
            raise
        except Exception:
            _fail("speech_outcome_unconfirmed", 502)

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
