"""Give a character one of our own voices as its timbre sample (#1222).

The video model follows a character's voice only from a sample that is a clip
in the story: storyboard compile sends `voices[]`, and a catalog choice alone
(`data.voiceId`) sent nothing. Here the catalog's own sample file becomes a
story clip, once per story, voice and language, and the character's main
voice. No provider is called and nothing is charged.
"""

import io
import logging
from pathlib import Path

from fastapi import HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, StrictStr

from .repository import Conflict
from .speech_catalog import LANGUAGES, VOICES

SAMPLES = Path(__file__).parent / "voice_samples"
LOG = logging.getLogger(__name__)


class VoiceBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    voice_id: StrictStr = Field(min_length=1, max_length=40)
    language: StrictStr = Field(pattern=r"^(zh|en)$")


class CatalogVoiceError(Exception):
    def __init__(self, code, status):
        super().__init__(code)
        self.code, self.status = code, status


def choice(value):
    """A catalog voice id a model or a client named, or None."""
    return value if isinstance(value, str) and value in VOICES else None


def needs_sample(character):
    """A character with a catalog voice chosen and no sample to send yet."""
    data = character.get("data", {})
    return choice(data.get("voiceId")) is not None and not data.get("voices")


def attach(app, principal, project_id, character_id, voice_id, language):
    """Make the voice's sample the character's main voice; the same call twice adds nothing."""
    if voice_id not in VOICES or language not in LANGUAGES:
        raise CatalogVoiceError("voice_unknown", 404)
    clip = f"catalog-voice-{voice_id}-{language}"
    for _ in range(3):
        row = app.state.authorized_project(principal, project_id, "projects:write")
        entities = row["payload"]["entities"]
        card = next((e for e in entities if e["id"] == character_id), None)
        if card is None or card.get("type") != "character":
            raise CatalogVoiceError("voice_character_missing", 404)
        data = card["data"]
        voices = list(data.get("voices", []))
        entry = next((v for v in voices if v.get("assetId") == clip), None)
        actions = []
        if not any(e["id"] == clip for e in entities):
            asset = app.state.assets.upload(
                principal.owner,
                project_id,
                io.BytesIO((SAMPLES / f"{voice_id}-{language}.mp3").read_bytes()),
                f"{voice_id}-{language}.mp3",
                client_asset_id=f"catalog-voice:{voice_id}:{language}",
            )
            if asset.get("status") != "ready":
                raise CatalogVoiceError("voice_store_failed", 502)
            actions.append(
                {
                    "op": "asset.attach",
                    "asset_id": asset["asset_id"],
                    "entity_id": clip,
                    "title": VOICES[voice_id]["name"][language],
                }
            )
        if entry is None:
            entry = {
                "id": f"catalog-{voice_id}-{language}",
                "assetId": clip,
                "clipStart": 0,
            }
            voices.append(entry)
        patch = {"voices": voices, "voiceId": voice_id, "mainVoiceId": entry["id"]}
        if not actions and all(data.get(key) == value for key, value in patch.items()):
            return entry
        actions.append(
            {"op": "entity.update", "entity_id": character_id, "patch": {"data": patch}}
        )
        try:
            app.state.guided.mutate(
                principal,
                project_id,
                {"expected_version": row["version"], "actions": actions},
            )
        except Conflict as error:
            if str(error) == "document_version_conflict":
                continue
            raise
        return entry
    raise CatalogVoiceError("document_version_conflict", 409)


def enabled(app):
    """The tenant's voice switch; off means our voices are not offered at all."""
    from .speech_settings import read

    with app.state.repository.engine.connect() as connection:
        return read(connection, app.state.settings.tenant_id)["voice_enabled"] is True


def after_story_chat(app, principal, project_id, language, changes):
    """Samples for the characters a chat turn just gave a voice to.

    The turn is already saved: a failure here leaves a character without a
    sample, which the readiness check reports, and never fails the turn.
    """
    idents = {c["id"] for c in changes if c.get("kind") == "character" and "id" in c}
    if not idents:
        return
    try:
        if not enabled(app):
            return
        row = app.state.authorized_project(principal, project_id, "projects:write")
        for card in row["payload"]["entities"]:
            if card["id"] in idents and needs_sample(card):
                attach(
                    app,
                    principal,
                    project_id,
                    card["id"],
                    card["data"]["voiceId"],
                    language if language in LANGUAGES else "zh",
                )
    except Exception as error:
        LOG.warning("catalog_voice_unavailable error_type=%s", type(error).__name__)


def register_routes(app):
    @app.post("/v1/projects/{project_id}/characters/{character_id}/catalog-voice")
    def give_voice(
        project_id: str, character_id: str, body: VoiceBody, request: Request
    ):
        from .generation_admission import reject_managed

        principal = request.state.principal
        row = app.state.authorized_project(principal, project_id, "projects:write")
        reject_managed(row["payload"])
        if not enabled(app):
            raise HTTPException(409, {"code": "voice_disabled"})
        try:
            entry = attach(
                app, principal, project_id, character_id, body.voice_id, body.language
            )
        except CatalogVoiceError as error:
            raise HTTPException(error.status, {"code": error.code}) from None
        return {"voice": entry}
