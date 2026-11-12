"""Our voices (#997). Stories keep these ids; only this table names who speaks them.

Changing provider is a change here, with no story migration. A clip records the voice
id and its `revision`; `maker()` turns that pair back into the vendor, model and vendor
voice, so stories never carry them (#1097). To change who speaks a voice, move its
current vendor fields into RETIRED under the old revision, then bump `revision`.
Nothing past `public()` may reach a person: users see voice names, never a vendor.

Two kinds of voice (#1226). A spoken voice has vendor fields and can say a line. A designed
voice has none: it is a timbre sample only (`voice_samples/`), made once with our own voice
design, and is what a shot sends so the character sounds the same. Every voice has a
`gender` and an `age` band; the name carries the manner.
"""

ELEVEN = {"provider": "openrouter", "model": "elevenlabs/eleven-v4"}
LANGUAGES = ("zh", "en")
GENDERS = ("female", "male")
AGES = ("child", "teen", "young", "adult", "senior")

VOICES = {
    "v-steady": {
        "name": {"zh": "沉稳", "en": "Steady"},
        "gender": "female",
        "age": "young",
        "provider_voice": "sarah",
        "revision": "r1",
        **ELEVEN,
    },
    "v-velvet": {
        "name": {"zh": "丝绒", "en": "Velvet"},
        "gender": "female",
        "age": "adult",
        "provider_voice": "lily",
        "revision": "r1",
        **ELEVEN,
    },
    "v-bright": {
        "name": {"zh": "明亮", "en": "Bright"},
        "gender": "female",
        "age": "young",
        "provider_voice": "jessica",
        "revision": "r1",
        **ELEVEN,
    },
    "v-clear": {
        "name": {"zh": "清晰", "en": "Clear"},
        "gender": "female",
        "age": "adult",
        "provider_voice": "alice",
        "revision": "r1",
        **ELEVEN,
    },
    "v-warm": {
        "name": {"zh": "温厚", "en": "Warm"},
        "gender": "male",
        "age": "adult",
        "provider_voice": "george",
        "revision": "r1",
        **ELEVEN,
    },
    "v-deep": {
        "name": {"zh": "低沉", "en": "Deep"},
        "gender": "male",
        "age": "adult",
        "provider_voice": "brian",
        "revision": "r1",
        **ELEVEN,
    },
    "v-anchor": {
        "name": {"zh": "端正", "en": "Anchor"},
        "gender": "male",
        "age": "adult",
        "provider_voice": "daniel",
        "revision": "r1",
        **ELEVEN,
    },
    "v-young": {
        "name": {"zh": "少年", "en": "Young"},
        "gender": "male",
        "age": "young",
        "provider_voice": "liam",
        "revision": "r1",
        **ELEVEN,
    },
}

# (voice id, revision) -> vendor fields of revisions that are no longer current.
RETIRED = {}
MAKER_FIELDS = ("provider", "model", "provider_voice")


def spoken(voice_id):
    """The voice if it can say a line; a designed voice is a sample only."""
    voice = VOICES.get(voice_id)
    return voice if voice is not None and "provider" in voice else None


def maker(voice_id, revision):
    """Who made a clip of this voice revision, or None if the pair never existed."""
    current = spoken(voice_id)
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
            "age": voice["age"],
            "speaks": "provider" in voice,
            "samples": {
                language: f"/v1/voices/{ident}/sample/{language}"
                for language in LANGUAGES
            },
        }
        for ident, voice in VOICES.items()
    ]
