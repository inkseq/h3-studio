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


def clip_id(voice_id, language):
    return f"catalog-voice-{voice_id}-{language}"


def entry(voice_id, language):
    """The character's `voices[]` item for this sample."""
    return {
        "id": f"catalog-{voice_id}-{language}",
        "assetId": clip_id(voice_id, language),
        "clipStart": 0,
    }


def enabled(app):
    """The tenant's voice switch; off means our voices are not offered at all."""
    from .speech_settings import read

    with app.state.repository.engine.connect() as connection:
        return read(connection, app.state.settings.tenant_id)["voice_enabled"] is True


def clip_action(app, principal, project_id, voice_id, language):
    """The action that makes the sample a story clip; stores the file if needed."""
    asset = app.state.assets.upload(
        principal.owner,
        project_id,
        io.BytesIO((SAMPLES / f"{voice_id}-{language}.mp3").read_bytes()),
        f"{voice_id}-{language}.mp3",
        client_asset_id=f"catalog-voice:{voice_id}:{language}",
    )
    if asset.get("status") != "ready":
        raise CatalogVoiceError("voice_store_failed", 502)
    return {
        "op": "asset.attach",
        "asset_id": asset["asset_id"],
        "entity_id": clip_id(voice_id, language),
        "title": VOICES[voice_id]["name"][language],
    }


def attach(app, principal, project_id, character_id, voice_id, language):
    """Make the voice's sample the character's main voice; the same call twice adds nothing."""
    if voice_id not in VOICES or language not in LANGUAGES:
        raise CatalogVoiceError("voice_unknown", 404)
    clip, sample = clip_id(voice_id, language), entry(voice_id, language)
    for _ in range(3):
        row = app.state.authorized_project(principal, project_id, "projects:write")
        entities = row["payload"]["entities"]
        card = next((e for e in entities if e["id"] == character_id), None)
        if card is None or card.get("type") != "character":
            raise CatalogVoiceError("voice_character_missing", 404)
        data = card["data"]
        voices = list(data.get("voices", []))
        if not any(v.get("id") == sample["id"] for v in voices):
            voices.append(sample)
        actions = []
        if not any(e["id"] == clip for e in entities):
            actions.append(clip_action(app, principal, project_id, voice_id, language))
        patch = {"voices": voices, "voiceId": voice_id, "mainVoiceId": sample["id"]}
        if not actions and all(data.get(key) == value for key, value in patch.items()):
            return sample
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
        return sample
    raise CatalogVoiceError("document_version_conflict", 409)


def _named(action):
    """The character data of a create or update that names a voice and has no sample."""
    if action.get("op") == "entity.create":
        data = action["entity"].get("data")
    elif action.get("op") == "entity.update":
        data = action.get("patch", {}).get("data")
    else:
        data = None
    if (
        isinstance(data, dict)
        and choice(data.get("voiceId"))
        and not data.get("voices")
    ):
        return data
    return None


def with_samples(app, principal, project_id, project, actions, language):
    """A story chat turn's actions, with a sample for each voice it names.

    Part of the turn's own write, so undo takes the voice back with the turn.
    Anything that goes wrong leaves the actions as they were: the character
    keeps its choice without a sample, which the readiness check reports.
    """
    language = language if language in LANGUAGES else "zh"
    try:
        if not any(_named(action) for action in actions) or not enabled(app):
            return actions
        app.state.authorized_project(principal, project_id, "assets:write")
        import copy

        result = copy.deepcopy(actions)
        named = [data for data in map(_named, result) if data is not None]
        held = {entity["id"] for entity in project["entities"]}
        clips = [
            clip_action(app, principal, project_id, voice_id, language)
            for voice_id in dict.fromkeys(data["voiceId"] for data in named)
            if clip_id(voice_id, language) not in held
        ]
        for data in named:
            sample = entry(data["voiceId"], language)
            data.update(voices=[sample], mainVoiceId=sample["id"])
        return clips + result
    except Exception as error:
        LOG.warning("catalog_voice_unavailable error_type=%s", type(error).__name__)
        return actions


def register_routes(app):
    @app.post("/v1/projects/{project_id}/characters/{character_id}/catalog-voice")
    def give_voice(
        project_id: str, character_id: str, body: VoiceBody, request: Request
    ):
        from .generation_admission import reject_managed

        principal = request.state.principal
        row = app.state.authorized_project(principal, project_id, "projects:write")
        # The sample is stored as a file of the story, like any upload.
        app.state.authorized_project(principal, project_id, "assets:write")
        reject_managed(row["payload"])
        if not enabled(app):
            raise HTTPException(409, {"code": "voice_disabled"})
        try:
            sample = attach(
                app, principal, project_id, character_id, body.voice_id, body.language
            )
        except CatalogVoiceError as error:
            raise HTTPException(error.status, {"code": error.code}) from None
        except HTTPException:
            raise
        except Exception as error:
            # A trashed or changed sample file, or a clip kept under another name.
            LOG.warning("catalog_voice_failed error_type=%s", type(error).__name__)
            raise HTTPException(409, {"code": "voice_store_failed"}) from None
        return {"voice": sample}
