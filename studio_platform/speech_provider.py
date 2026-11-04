"""Speech adapters (#997): one small interface, so a provider is one class.

`synthesize(text, voice, direction, language)` returns audio and usage. A catalog voice
names its provider, and a voice never moves to another provider on failure: a different
provider is a different voice, so an outage fails clearly (owner, 2026-11-04).
"""

from dataclasses import dataclass
from pathlib import Path
import tempfile

import httpx

from . import media
from .protected_config import read_protected_json

DIRECTIONS = ("calm", "angry", "sad", "happy")
ORIGIN = "https://openrouter.ai/api/v1"
PROFILE = "openrouter--inkseq-chat"
MAX_AUDIO_BYTES = 8 * 1024 * 1024


class SpeechError(Exception):
    def __init__(self, code, status=502):
        self.code, self.status = code, status
        super().__init__(code)


class SpeechConfigError(ValueError):
    def __init__(self):
        super().__init__("speech_config_invalid")


@dataclass(frozen=True)
class Credential:
    api_key: str = ""

    def __repr__(self):
        return "Credential(...)"


@dataclass(frozen=True)
class Speech:
    data: bytes
    characters: int


class OpenRouterSpeech:
    """Eleven v4 through OpenRouter. Delivery is written as bracket tags in the text."""

    NAME = "openrouter"
    STYLE = {
        "calm": "quietly",
        "angry": "furious, shouting",
        "sad": "crying, voice breaking",
        "happy": "excited, laughing",
    }

    def __init__(self, credential, transport=None):
        self.credential, self.transport = credential, transport

    def synthesize(self, text, voice, direction, language):
        # The model reads the language from the text; other adapters may need `language`.
        from .llm_tracing import clip, generation, usage

        spoken = f"[{self.STYLE[direction]}] {text}"
        with generation(
            "speech", model=voice["model"], input=clip(spoken, 2000)
        ) as traced:
            data = self._post(voice, spoken)
            traced.update(
                output={"bytes": len(data)},
                usage_details=usage(len(spoken), 0, 0),
            )
        return Speech(data, len(spoken))

    def _post(self, voice, spoken):
        try:
            with httpx.Client(
                timeout=httpx.Timeout(60, connect=10),
                follow_redirects=False,
                transport=self.transport,
                trust_env=False,
            ) as client:
                reply = client.post(
                    ORIGIN + "/audio/speech",
                    json={
                        "model": voice["model"],
                        "voice": voice["provider_voice"],
                        "response_format": "mp3",
                        "input": spoken,
                    },
                    headers={"Authorization": "Bearer " + self.credential.api_key},
                )
        except httpx.HTTPError:
            raise SpeechError("speech_provider_unavailable") from None
        if reply.status_code != 200:
            raise SpeechError("speech_provider_failed")
        if not reply.content or len(reply.content) > MAX_AUDIO_BYTES:
            raise SpeechError("speech_provider_failed")
        return reply.content


def validate(value):
    if (
        not isinstance(value, dict)
        or set(value) != {"enabled", "openrouter"}
        or value["enabled"] is not True
    ):
        raise SpeechConfigError()
    config = value["openrouter"]
    if (
        not isinstance(config, dict)
        or set(config) != {"profile", "base_url", "api_key"}
        or config["profile"] != PROFILE
        or config["base_url"] != ORIGIN
        or not isinstance(config["api_key"], str)
        or not 16 <= len(config["api_key"]) <= 1024
        or any(c.isspace() or ord(c) < 32 for c in config["api_key"])
    ):
        raise SpeechConfigError()
    return Credential(config["api_key"])


def configured(settings):
    """The credential the host installed into the data volume; absent or bad means off."""
    path = Path(settings.data_dir) / "runtime-secrets" / "speech.json"
    if not path.is_file():
        return None
    try:
        credential = validate(read_protected_json(path, SpeechConfigError))
    except SpeechConfigError:
        return None
    return {OpenRouterSpeech.NAME: OpenRouterSpeech(credential)}


def level(data):
    """Same loudness for every clip, whichever voice or delivery made it."""
    with tempfile.TemporaryDirectory(prefix="inkseq-speech-") as folder:
        source, target = Path(folder) / "line.mp3", Path(folder) / "level.mp3"
        source.write_bytes(data)
        try:
            media.ffmpeg(
                [
                    "-i",
                    source,
                    "-af",
                    "loudnorm=I=-16:TP=-1.5:LRA=11",
                    "-ar",
                    44100,
                    "-b:a",
                    "128k",
                    target,
                ],
                timeout=60,
            )
        except media.MediaError:
            raise SpeechError("speech_audio_failed") from None
        return target.read_bytes()
