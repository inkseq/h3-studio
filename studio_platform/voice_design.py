"""Character voice design (owner, 2026-11-07): a voice brief from the card, three previews.

The person describes a voice in words (Inky drafts it from the character card). The
provider designs it, and one chosen preview becomes the character's voice sample, the
audio reference that keeps a character's voice the same across shots.

Free while new; a per-account daily cap bounds the provider bill. Credentials:
`runtime-secrets/voice-design.json` in the data volume, installed by the host like the
speech credential. Absent or invalid means the feature is off. People never see the
provider's name.
"""

from dataclasses import dataclass
import json
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from .prompt_files import text as prompt_text
from .protected_config import read_protected_json

PROFILE = "fal--inkseq"
ENDPOINT = "https://fal.run/fal-ai/qwen-3-tts/voice-design/1.7b"
FILENAME = "voice-design.json"
LANGUAGES = {"zh": "Chinese", "en": "English"}
MAX_AUDIO_BYTES = 4 * 1024 * 1024
MAX_DESCRIPTION = 400
MAX_SAMPLE = 120
BRIEF = prompt_text("voice_brief")
# Calm defaults when no brief can be drafted; the provider refuses violent lines.
SAMPLE = {
    "zh": "天快黑了，我们早点回家吧。路上小心，别着急。",
    "en": "It's getting dark. Let's head home early, and take care on the way.",
}


class VoiceDesignError(Exception):
    def __init__(self, code, status=502):
        self.code, self.status = code, status
        super().__init__(code)


class VoiceDesignConfigError(ValueError):
    def __init__(self):
        super().__init__("voice_design_config_invalid")


@dataclass(frozen=True)
class Credential:
    api_key: str = ""

    def __repr__(self):
        return "Credential(...)"


def validate(value):
    if (
        not isinstance(value, dict)
        or set(value) != {"enabled", "fal"}
        or value["enabled"] is not True
    ):
        raise VoiceDesignConfigError()
    config = value["fal"]
    if (
        not isinstance(config, dict)
        or set(config) != {"profile", "api_key"}
        or config["profile"] != PROFILE
        or not isinstance(config["api_key"], str)
        or not 16 <= len(config["api_key"]) <= 1024
        or any(c.isspace() or ord(c) < 32 for c in config["api_key"])
    ):
        raise VoiceDesignConfigError()
    return Credential(config["api_key"])


def configured(settings):
    """The designer the host installed a credential for, or None (feature off)."""
    path = Path(settings.data_dir) / "runtime-secrets" / FILENAME
    if not path.is_file():
        return None
    try:
        credential = validate(read_protected_json(path, VoiceDesignConfigError))
    except VoiceDesignConfigError:
        return None
    return FalVoiceDesign(credential)


def _media_url(value):
    """Only the provider's own media hosts, over https, may be downloaded."""
    if not isinstance(value, str) or len(value) > 2048:
        return None
    parts = urlsplit(value)
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or not (
        host == "fal.media" or host.endswith(".fal.media")
    ):
        return None
    return value


class FalVoiceDesign:
    """Qwen3-TTS voice design on fal: words in, one spoken preview out."""

    def __init__(self, credential, transport=None):
        self.credential, self.transport = credential, transport

    def design(self, description, text, language):
        from .llm_tracing import clip, generation, usage

        with generation(
            "voice-design",
            model="qwen-3-tts-voice-design",
            input=clip(description, 500),
        ) as traced:
            data = self._design(description, text, language)
            traced.update(
                output={"bytes": len(data)}, usage_details=usage(len(text), 0, 0)
            )
        return data

    def _client(self, timeout):
        return httpx.Client(
            timeout=httpx.Timeout(timeout, connect=10),
            follow_redirects=False,
            transport=self.transport,
            trust_env=False,
        )

    def _design(self, description, text, language):
        try:
            with self._client(120) as client:
                reply = client.post(
                    ENDPOINT,
                    json={
                        "text": text,
                        "prompt": description,
                        "language": LANGUAGES[language],
                    },
                    headers={"Authorization": "Key " + self.credential.api_key},
                )
                if reply.status_code == 422 and "content_policy" in reply.text:
                    raise VoiceDesignError("voice_design_refused", 422)
                if reply.status_code != 200:
                    raise VoiceDesignError("voice_design_failed")
                url = _media_url((reply.json().get("audio") or {}).get("url"))
                if url is None:
                    raise VoiceDesignError("voice_design_failed")
                audio = client.get(url)
        except VoiceDesignError:
            raise
        except (httpx.HTTPError, ValueError, AttributeError):
            raise VoiceDesignError("voice_design_unavailable") from None
        if (
            audio.status_code != 200
            or not audio.content
            or len(audio.content) > MAX_AUDIO_BYTES
        ):
            raise VoiceDesignError("voice_design_failed")
        return audio.content


def _clean(value, limit):
    if not isinstance(value, str):
        return ""
    value = " ".join(value.split())
    return value if 0 < len(value) <= limit else ""


def brief(client, card, speaker, language):
    """Inky's draft of a voice for this card: {"description", "sample"}.

    Falls back to the card's own text and a calm line when the model is unavailable,
    so the person can still edit the words and design.
    """
    fallback = {
        "description": _clean(
            f"{card.get('title', '')}: {card.get('description', '')}", MAX_DESCRIPTION
        )
        or card.get("title", "")[:MAX_DESCRIPTION],
        "sample": SAMPLE[language],
    }
    if client is None:
        return fallback
    source = {
        "language": language,
        "name": str(card.get("title", ""))[:160],
        "description": str(card.get("description", ""))[:2000],
        "speaker": speaker[:80],
    }
    try:
        response = client.generate(
            "gemini-3.8-flash",
            [{"role": "user", "text": json.dumps(source, ensure_ascii=False)}],
            instruction=BRIEF,
            # Thinking spends output tokens; 512 cut the JSON off on staging.
            max_tokens=2048,
            json_mode=True,
            thinking="low",
        )
        value = json.loads(response.get("text") or "")
    except Exception:
        return fallback
    description = _clean(
        value.get("description") if isinstance(value, dict) else None, MAX_DESCRIPTION
    )
    sample = _clean(
        value.get("sample") if isinstance(value, dict) else None, MAX_SAMPLE
    )
    return {
        "description": description or fallback["description"],
        "sample": sample or fallback["sample"],
    }
