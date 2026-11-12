"""Whether a shot has the material to generate well (docs/STORY-AUTO-QUEUE.md).

The deterministic first layer: free, instant and with no model call. Text-only
shots invent a new person every render and unscripted speech comes out as
gibberish (#918), so a recurring person needs a picture or a first frame, and a
shot with sound needs written lines or an explicit "no dialogue". A click only
gets a prompt from these findings; the economy auto-queue requires none.
"""

import re

# Same signal as the frontend's submitHints: H3 speech tags (what Quick Create
# writes), quoted lines or "Name: line".
DIALOGUE = re.compile(r"<d>|[“”\"「」『』]|[：:]\s*\S", re.IGNORECASE)
# A shot sends this many voice samples (capabilities "max_audios"); cast order decides.
MAX_VOICES = 3
# Who speaks: the video model's speaker tag, as story chat writes it.
SPEAKER = re.compile(r"\(\s*(S\d+)\s*\)", re.IGNORECASE)
NO_DIALOGUE = re.compile(
    r"no dialogue|no speech|without speech|silent|无对白|没有对白|无台词|没有台词|不说话|静音",
    re.IGNORECASE,
)


def _look(character, binding):
    """The look this shot uses, by the same rule as compile_draft."""
    data = character.get("data", {})
    selected = binding.get("lookId") if isinstance(binding, dict) else ""
    ident = selected or data.get("defaultLookId")
    looks = data.get("looks", [])
    match = next((look for look in looks if look.get("id") == ident), None)
    return match or (looks[0] if not ident and looks else None)


def _voice(character, look):
    """The sample compile_draft would send for this character, if it has one."""
    data = character.get("data", {})
    ident = (look or {}).get("voiceId") or data.get("mainVoiceId")
    return next((v for v in data.get("voices", []) if v.get("id") == ident), None)


def _speakers(action, voiceless):
    """Names among the voiceless cast who have a line in this text.

    A numbered speaker counts only when its tag is in the text. Without tags,
    or for a character with no number, any spoken line counts.
    """
    if (
        not voiceless
        or NO_DIALOGUE.search(action)
        or not re.search(r"<d>", action, re.I)
    ):
        return []
    tags = {tag.upper() for tag in SPEAKER.findall(action)}
    names = []
    for character in voiceless:
        number = str(character.get("data", {}).get("speaker") or "").upper()
        if tags and number and number not in tags:
            continue
        if character.get("title") not in names:
            names.append(character.get("title"))
    return names


def findings(project, shot):
    """Closed codes for what this shot is missing; empty means nothing is."""
    lookup = {entity["id"]: entity for entity in project.get("entities", [])}
    data = shot.get("data", {})
    scene = lookup.get(shot.get("parentId"), {})
    board = data.get("storyboard", {})
    action = data.get("prompt", shot.get("description", "")) or ""
    result = []
    if not action.strip():
        result.append({"code": "action_missing"})
    has_frame = bool(board.get("firstFrameId") or board.get("continuePrev"))
    missing, voiceless, voiced = [], [], {}
    for binding in data.get("cast", scene.get("data", {}).get("cast", [])):
        ident = binding if isinstance(binding, str) else binding.get("characterId")
        character = lookup.get(ident)
        if character is None or character.get("type") != "character":
            continue
        look = _look(character, binding)
        if not (look or {}).get("imageIds") and character.get("title") not in missing:
            missing.append(character.get("title"))
        sample = _voice(character, look)
        if not sample:
            voiceless.append(character)
        else:
            # Two people with the same clip send it once.
            clip = (sample.get("assetId"), sample.get("clipStart", 0))
            voiced.setdefault(clip, character.get("title"))
    if missing and not has_frame:
        result.append({"code": "character_picture_missing", "names": missing})
    audio = data.get("h3", {}).get("controls", {}).get("generate_audio") is not False
    names = _speakers(action, voiceless) if audio else []
    # Samples go out with reference shots only, and a finished track carries
    # every voice itself (storyboard.compile_draft).
    sends = audio and board.get("mode") == "ref" and not board.get("dialogueTrack")
    if names and sends:
        result.append({"code": "voice_missing", "names": names})
    if sends and len(voiced) > MAX_VOICES:
        dropped = list(voiced.values())[MAX_VOICES:]
        result.append({"code": "voice_dropped", "names": dropped})
    if (
        audio
        and action.strip()
        and not DIALOGUE.search(action)
        and not NO_DIALOGUE.search(action)
    ):
        result.append({"code": "speech_unspecified"})
    return result


def project_readiness(project):
    """Per-shot findings for one project document; read-only."""
    items = []
    for shot in project.get("entities", []):
        if shot.get("type") != "shot":
            continue
        found = findings(project, shot)
        items.append(
            {
                "shot_id": shot["id"],
                "base_version": shot.get("version", 1),
                "ready": not found,
                "findings": found,
            }
        )
    return {"items": items}
