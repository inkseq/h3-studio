"""Our voices (#997). Stories keep these ids; only this table names who speaks them.

Changing provider is a change here, with no story migration. A clip records the voice
id and its `revision`; `maker()` turns that pair back into the vendor, model and vendor
voice, so stories never carry them (#1097). To change who speaks a voice, move its
current vendor fields into RETIRED under the old revision, then bump `revision`.
Nothing past `public()` may reach a person: users see voice names, never a vendor.
"""

ELEVEN = {"provider": "openrouter", "model": "elevenlabs/eleven-v4"}
LANGUAGES = ("zh", "en")

VOICES = {
    "v-steady": {
        "name": {"zh": "沉稳", "en": "Steady"},
        "gender": "female",
        "provider_voice": "sarah",
        "revision": "r1",
        **ELEVEN,
    },
    "v-velvet": {
        "name": {"zh": "丝绒", "en": "Velvet"},
        "gender": "female",
        "provider_voice": "lily",
        "revision": "r1",
        **ELEVEN,
    },
    "v-bright": {
        "name": {"zh": "明亮", "en": "Bright"},
        "gender": "female",
        "provider_voice": "jessica",
        "revision": "r1",
        **ELEVEN,
    },
    "v-clear": {
        "name": {"zh": "清晰", "en": "Clear"},
        "gender": "female",
        "provider_voice": "alice",
        "revision": "r1",
        **ELEVEN,
    },
    "v-warm": {
        "name": {"zh": "温厚", "en": "Warm"},
        "gender": "male",
        "provider_voice": "george",
        "revision": "r1",
        **ELEVEN,
    },
    "v-deep": {
        "name": {"zh": "低沉", "en": "Deep"},
        "gender": "male",
        "provider_voice": "brian",
        "revision": "r1",
        **ELEVEN,
    },
    "v-anchor": {
        "name": {"zh": "端正", "en": "Anchor"},
        "gender": "male",
        "provider_voice": "daniel",
        "revision": "r1",
        **ELEVEN,
    },
    "v-young": {
        "name": {"zh": "少年", "en": "Young"},
        "gender": "male",
        "provider_voice": "liam",
        "revision": "r1",
        **ELEVEN,
    },
}

# (voice id, revision) -> vendor fields of revisions that are no longer current.
RETIRED = {}
MAKER_FIELDS = ("provider", "model", "provider_voice")


def maker(voice_id, revision):
    """Who made a clip of this voice revision, or None if the pair never existed."""
    current = VOICES.get(voice_id)
    if current is not None and current["revision"] == revision:
        return {key: current[key] for key in MAKER_FIELDS}
    retired = RETIRED.get((voice_id, revision))
    return dict(retired) if retired is not None else None


def public():
    """What the app may show. Samples are made once and served by id, not by vendor."""
    return [
        {
            "id": ident,
            "name": voice["name"],
            "gender": voice["gender"],
            "samples": {
                language: f"/v1/voices/{ident}/sample/{language}"
                for language in LANGUAGES
            },
        }
        for ident, voice in VOICES.items()
    ]
