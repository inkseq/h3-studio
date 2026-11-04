"""Our voices (#997). Stories keep these ids; only this table names who speaks them.

Changing provider is a change here, with no story migration. Finished clips keep what
made them in their own provenance, so they never change. Nothing past `public()` may
reach a person: users see voice names, never a vendor, model or vendor voice.
"""

ELEVEN = {"provider": "openrouter", "model": "elevenlabs/eleven-v4"}
LANGUAGES = ("zh", "en")

VOICES = {
    "v-steady": {
        "name": {"zh": "沉稳", "en": "Steady"},
        "gender": "female",
        "provider_voice": "sarah",
        **ELEVEN,
    },
    "v-velvet": {
        "name": {"zh": "丝绒", "en": "Velvet"},
        "gender": "female",
        "provider_voice": "lily",
        **ELEVEN,
    },
    "v-bright": {
        "name": {"zh": "明亮", "en": "Bright"},
        "gender": "female",
        "provider_voice": "jessica",
        **ELEVEN,
    },
    "v-clear": {
        "name": {"zh": "清晰", "en": "Clear"},
        "gender": "female",
        "provider_voice": "alice",
        **ELEVEN,
    },
    "v-warm": {
        "name": {"zh": "温厚", "en": "Warm"},
        "gender": "male",
        "provider_voice": "george",
        **ELEVEN,
    },
    "v-deep": {
        "name": {"zh": "低沉", "en": "Deep"},
        "gender": "male",
        "provider_voice": "brian",
        **ELEVEN,
    },
    "v-anchor": {
        "name": {"zh": "端正", "en": "Anchor"},
        "gender": "male",
        "provider_voice": "daniel",
        **ELEVEN,
    },
    "v-young": {
        "name": {"zh": "少年", "en": "Young"},
        "gender": "male",
        "provider_voice": "liam",
        **ELEVEN,
    },
}


def public():
    """What the app may show. Samples are static files of the web build."""
    return [
        {
            "id": ident,
            "name": voice["name"],
            "gender": voice["gender"],
            "samples": {
                language: f"/voices/{ident}-{language}.mp3" for language in LANGUAGES
            },
        }
        for ident, voice in VOICES.items()
    ]
