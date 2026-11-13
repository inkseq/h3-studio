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
    "v-sassy": {
        "name": {"zh": "俏皮", "en": "Sassy"},
        "gender": "female",
        "age": "young",
        "provider_voice": "laura",
        "revision": "r1",
        **ELEVEN,
    },
    "v-easy": {
        "name": {"zh": "从容", "en": "Easygoing"},
        "gender": "male",
        "age": "adult",
        "provider_voice": "roger",
        "revision": "r1",
        **ELEVEN,
    },
    "v-chill": {
        "name": {"zh": "随和", "en": "Chill"},
        "gender": "male",
        "age": "young",
        "provider_voice": "will",
        "revision": "r1",
        **ELEVEN,
    },
    "v-casual": {
        "name": {"zh": "自然", "en": "Casual"},
        "gender": "male",
        "age": "adult",
        "provider_voice": "chris",
        "revision": "r1",
        **ELEVEN,
    },
    "v-friendly": {
        "name": {"zh": "亲切", "en": "Friendly"},
        "gender": "female",
        "age": "adult",
        "provider_voice": "matilda",
        "revision": "r1",
        **ELEVEN,
    },
    "v-energetic": {
        "name": {"zh": "活力", "en": "Energetic"},
        "gender": "male",
        "age": "young",
        "provider_voice": "charlie",
        "revision": "r1",
        **ELEVEN,
    },
    "v-gravel": {
        "name": {"zh": "粗粝", "en": "Gravelly"},
        "gender": "male",
        "age": "adult",
        "provider_voice": "callum",
        "revision": "r1",
        **ELEVEN,
    },
    "v-composed": {
        "name": {"zh": "稳重", "en": "Composed"},
        "gender": "male",
        "age": "adult",
        "provider_voice": "eric",
        "revision": "r1",
        **ELEVEN,
    },
    "v-seasoned": {
        "name": {"zh": "老练", "en": "Seasoned"},
        "gender": "male",
        "age": "senior",
        "provider_voice": "bill",
        "revision": "r1",
        **ELEVEN,
    },
}

# Designed voices (#1226): id, zh name, en name, gender, age. Samples only, no vendor.
DESIGNED = (
    ("vf-girl", "小女孩", "Little girl", "female", "child"),
    ("vf-shy-girl", "怯生生的女孩", "Shy girl", "female", "child"),
    ("vf-lively", "活泼少女", "Lively teen", "female", "teen"),
    ("vf-cool", "清冷少女", "Cool teen", "female", "teen"),
    ("vf-sweet", "甜美", "Sweet", "female", "young"),
    ("vf-gentle", "温柔", "Gentle", "female", "young"),
    ("vf-crisp", "干练", "Crisp", "female", "young"),
    ("vf-hearty", "爽朗", "Hearty", "female", "young"),
    ("vf-husky", "烟嗓", "Husky", "female", "young"),
    ("vf-soft", "软糯", "Soft and sweet", "female", "young"),
    ("vf-icy", "冷艳", "Icy", "female", "adult"),
    ("vf-newsreader", "播音", "Newsreader", "female", "adult"),
    ("vf-mother", "温和的母亲", "Gentle mother", "female", "adult"),
    ("vf-fiery", "泼辣", "Fiery", "female", "adult"),
    ("vf-weary", "疲惫", "Weary", "female", "adult"),
    ("vf-elegant", "优雅", "Elegant", "female", "adult"),
    ("vf-stern", "严厉", "Stern", "female", "adult"),
    ("vf-storyteller", "讲述", "Storyteller", "female", "adult"),
    ("vf-big-sister", "北方大姐", "Big sister", "female", "adult"),
    ("vf-frail", "苍老", "Frail", "female", "senior"),
    ("vf-grandma", "慈祥的奶奶", "Kind grandma", "female", "senior"),
    ("vf-tough-granny", "厉害的老太太", "Tough old lady", "female", "senior"),
    ("vm-boy", "小男孩", "Little boy", "male", "child"),
    ("vm-cheeky-boy", "调皮男孩", "Cheeky boy", "male", "child"),
    ("vm-teen", "变声期少年", "Teen boy", "male", "teen"),
    ("vm-sunny", "阳光", "Sunny", "male", "young"),
    ("vm-refined", "温润", "Refined", "male", "young"),
    ("vm-aloof", "冷淡", "Aloof", "male", "young"),
    ("vm-lazy", "慵懒", "Lazy", "male", "young"),
    ("vm-timid", "怯懦", "Timid", "male", "young"),
    ("vm-magnetic", "磁性", "Magnetic", "male", "adult"),
    ("vm-hoarse", "沙哑", "Hoarse", "male", "adult"),
    ("vm-slick", "油滑", "Slick", "male", "adult"),
    ("vm-sinister", "阴沉", "Sinister", "male", "adult"),
    ("vm-scholar", "儒雅", "Scholarly", "male", "adult"),
    ("vm-soldier", "硬朗", "Tough", "male", "adult"),
    ("vm-weary", "疲惫", "Weary", "male", "adult"),
    ("vm-newsreader", "播音", "Newsreader", "male", "adult"),
    ("vm-narrator", "讲述", "Narrator", "male", "adult"),
    ("vm-commanding", "威严", "Commanding", "male", "adult"),
    ("vm-rugged", "粗犷", "Rugged", "male", "adult"),
    ("vm-big-brother", "东北大哥", "Big brother", "male", "adult"),
    ("vm-streetwise", "南方口音", "Street-smart", "male", "adult"),
    ("vm-honest", "憨厚", "Simple and honest", "male", "adult"),
    ("vm-stubborn", "倔老头", "Stubborn old man", "male", "senior"),
    ("vm-elder", "长者", "Elder", "male", "senior"),
    ("vm-grandpa", "慈祥的爷爷", "Kind grandpa", "male", "senior"),
    ("vm-frail", "苍老", "Frail", "male", "senior"),
)
VOICES.update(
    {
        ident: {"name": {"zh": zh, "en": en}, "gender": gender, "age": age}
        for ident, zh, en, gender, age in DESIGNED
    }
)

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
