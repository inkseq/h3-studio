"""Write a storyboard shot as H3's own full-reference prompt before it is sent.

H3 speaks Chinese description text aloud and draws it as subtitles: the 2026-11-07
Geryon run read card names and stage directions instead of the lines. English
scaffolding kept the lines (A/B in docs/EXPERIMENTS.md). So every storyboard shot
for our own H3 machines is rewritten into the six sections of MiniMax's
full-reference format, in English, with the pictures and voices numbered in the
order they are sent and the lines copied word for word inside ``<d>`` tags.

The person keeps editing their own words; this text is only what the model reads.
A rewrite that loses a line, leaks Chinese outside a line, or cites a missing
picture is refused and the original prompt is sent instead.
"""

import hashlib
import json
import re
import threading
from collections import OrderedDict

from .prompt_files import text as prompt_text
from .storyboard import look_images

INSTRUCTION = prompt_text("h3_reference_prompt")
MODEL = "gemini-3.8-flash"
VERSION = "h3-reference-v1"
SECTIONS = (
    "subject_definitions:",
    "summary:",
    "retention_analysis:",
    "detailed_description:",
    "overall_soundscape:",
    "non_diegetic_music:",
)
MAX_PROMPT = 12000
LINE = re.compile(r"<d>.*?</d>", re.S)
FENCE = re.compile(r"^```[a-z]*$", re.M)
SPOKEN = re.compile(r"(\(S\d+\))\s*(<d>.*?</d>)", re.S)
TASK = re.compile(r"\[([a-z +]+)\]")
TASKS = {
    "reference generation",
    "audio reference",
    "audio reuse",
    "keyframe completion",
    "video continuation",
}
LABEL = re.compile(r"<(Picture|Video|Audio)\s+(\d+)>")
_CACHE, _LOCK, _CACHE_SIZE = OrderedDict(), threading.Lock(), 512


def _wide(ch):
    point = ord(ch)
    return (
        0x2E80 <= point <= 0x9FFF  # CJK radicals, punctuation, kana, ideographs
        or 0xAC00 <= point <= 0xD7AF  # Hangul
        or 0xF900 <= point <= 0xFAFF
        or 0xFE30 <= point <= 0xFE4F
        or 0xFF00 <= point <= 0xFFEF  # full-width forms
    )


def leaks(text):
    """Chinese (or other wide) characters outside the spoken lines."""
    return any(_wide(ch) for ch in LINE.sub("", text))


def official(text):
    stripped = text.lstrip()
    return stripped.startswith(SECTIONS[0]) or stripped.startswith(
        "integrated_multimodal_description:"
    )


def _cards(project):
    return {e["id"]: e for e in project["entities"]}


def _owner_of(lookup, asset_id):
    """The card a sent picture or voice belongs to, as (kind, card, role)."""
    for entity in lookup.values():
        data = entity.get("data") or {}
        if entity["type"] == "character":
            for look in data.get("looks", []):
                if asset_id in look_images(look):
                    return "character", entity, "picture"
            if any(v.get("assetId") == asset_id for v in data.get("voices", [])):
                return "character", entity, "voice"
        elif entity["type"] == "location":
            images = list(data.get("referenceAssetIds", []))
            for version in data.get("versions", []):
                images += version.get("imageIds", [])
            if asset_id in images:
                return "place", entity, "picture"
            if (data.get("ambient") or {}).get("assetId") == asset_id:
                return "place", entity, "ambience"
        elif entity["type"] == "prop" and asset_id in data.get("imageIds", []):
            return "prop", entity, "picture"
    return "extra", None, "picture"


def source(project, shot, draft):
    """The facts the rewrite may use, or None when this shot is not rewritten."""
    board = draft.get("storyboard")
    action = (shot.get("data") or {}).get("prompt", shot.get("description", ""))
    inputs = draft.get("inputs") or {}
    if (
        not board
        or ((shot.get("data") or {}).get("storyboard") or {}).get("mode") != "ref"
        or not isinstance(action, str)
        or not action.strip()
        or official(action)
        or inputs.get("guides")
        or inputs.get("last_frame")
    ):
        return None
    lookup = _cards(project)
    soundtrack = (draft.get("controls") or {}).get("audio_conditioning") == "soundtrack"
    cards, order = {}, []

    def card(kind, entity):
        if entity["id"] not in cards:
            cards[entity["id"]] = {
                "kind": kind,
                "name": entity.get("title", ""),
                "description": entity.get("description", ""),
                "pictures": [],
                "voice": None,
            }
            order.append(entity["id"])
        return cards[entity["id"]]

    for part in board.get("prompt_parts", []):
        entity = lookup.get(part.get("entity_id"))
        if entity is not None:
            card(
                {"cast": "character", "place": "place", "prop": "prop"}[part["kind"]],
                entity,
            )
    pictures, audios, videos, extras = 0, 0, 0, []
    for ref in board.get("references", []):
        if not ref.get("sent") or ref.get("slot") not in {"images", "audios", "videos"}:
            continue
        entity = lookup.get(ref["entity_id"])
        if ref["slot"] == "images":
            pictures += 1
            label = f"<Picture {pictures}>"
        elif ref["slot"] == "audios":
            audios += 1
            label = f"<Audio {audios}>"
        else:
            videos += 1
            extras.append({"label": f"<Video {videos}>", "use": "reference video"})
            continue
        kind, owner, role = _owner_of(lookup, ref["entity_id"])
        if owner is None and soundtrack and ref["slot"] == "audios":
            extras.append(
                {
                    "label": label,
                    "use": "finished dialogue soundtrack, reused exactly",
                    "timing": (entity or {}).get("description", "")[:600],
                }
            )
        elif owner is None:
            extras.append(
                {
                    "label": label,
                    "use": "extra reference",
                    "title": (entity or {}).get("title", ""),
                }
            )
        elif role == "picture":
            card(kind, owner)["pictures"].append(label)
        elif role == "voice":
            card(kind, owner)["voice"] = label
        else:
            extras.append({"label": label, "use": "ambience of " + owner["title"]})
    if (
        len(inputs.get("images", [])) != pictures
        or len(inputs.get("audios", [])) != audios
    ):
        return None  # The sent order is unknown; never guess a picture number.
    parts = {p["kind"]: p["text"] for p in board.get("prompt_parts", [])}
    controls = draft.get("controls") or {}
    seconds = controls.get("duration") or (shot.get("data") or {}).get("seconds", 5)
    return {
        "seconds": seconds,
        "style": " ".join(filter(None, (parts.get("style"), parts.get("look")))),
        "camera": parts.get("camera", ""),
        "starts_from_first_frame": bool(inputs.get("first_frame")),
        "cards": [cards[ident] for ident in order],
        "extra_references": extras,
        "action": action.strip(),
        "soundtrack": soundtrack,
        "counts": {"Picture": pictures, "Audio": audios, "Video": videos},
    }


def problems(value, facts):
    """Why a rewrite cannot be sent; empty when it can."""
    found = []
    if not isinstance(value, str) or not value.strip():
        return ["empty"]
    at = -1
    for header in SECTIONS:
        index = value.find("\n" + header) if at >= 0 else value.find(header)
        if index <= at:
            found.append("section order: " + header)
            break
        at = index
    if not value.lstrip().startswith(SECTIONS[0]):
        found.append("must start with subject_definitions:")
    if leaks(value):
        found.append("Chinese text outside <d> lines")
    wanted = LINE.findall(facts["action"])
    got = LINE.findall(value)
    rest = iter(got)
    if not all(any(line == other for other in rest) for line in wanted):
        found.append("a <d> line is missing, changed or out of order")
    if any(line not in facts["action"] for line in got):
        found.append("a <d> line that is not in the source")
    found += _speaker_problems(value, facts["action"])
    summary = value.split("\nsummary:", 1)[-1].lstrip()
    kinds = TASK.match(summary)
    named = set(kinds.group(1).split(" + ")) if kinds else set()
    if (
        not kinds
        or not named <= TASKS
        or summary[kinds.end() :].lstrip().startswith("+")
        or (
            facts["counts"]["Audio"]
            and ("audio reuse" if facts.get("soundtrack") else "audio reference")
            not in named
        )
    ):
        found.append(
            "summary must start with the task types inside one pair of brackets, "
            "e.g. [reference generation + audio reference]"
        )
    for kind, number in LABEL.findall(value):
        if not 1 <= int(number) <= facts["counts"][kind]:
            found.append(f"<{kind} {number}> was not sent")
    for kind, count in facts["counts"].items():
        for number in range(1, count + 1):
            if f"<{kind} {number}>" not in value:
                found.append(f"<{kind} {number}> is never cited")
    if len(value) > MAX_PROMPT:
        found.append("too long")
    return found


def _speaker_problems(value, action):
    """Each source line keeps its (Sx) between the previous line and itself."""
    found, start = [], 0
    for match in SPOKEN.finditer(action):
        speaker, line = match.group(1), match.group(2)
        at = value.find(line, start)
        if at < 0:
            break  # Reported as a missing line already.
        if speaker not in value[start:at]:
            found.append(f"write {speaker} right before the line {line}")
        start = at + len(line)
    return found


def _tidy(value):
    # A blank line starts a new prompt under WanGP's multi-prompt modes.
    lines = [line.strip() for line in value.replace("\r", "").split("\n")]
    return "\n".join(line for line in lines if line)


def _key(facts):
    return hashlib.sha256(
        json.dumps([VERSION, INSTRUCTION, facts], sort_keys=True).encode()
    ).hexdigest()


def rewrite(client, facts, attempts=2):
    """The English full-reference prompt, or None when no valid one was written."""
    if client is None or facts is None:
        return None
    key = _key(facts)
    with _LOCK:
        if key in _CACHE:
            _CACHE.move_to_end(key)
            return _CACHE[key]
    request = {k: v for k, v in facts.items() if k != "counts"}
    messages = [{"role": "user", "text": json.dumps(request, ensure_ascii=False)}]
    for _ in range(attempts):
        try:
            # Plain text: in JSON mode Gemini broke long strings with stray raw newlines.
            response = client.generate(
                MODEL,
                messages,
                instruction=INSTRUCTION,
                max_tokens=8192,
                thinking="low",
            )
            value = _tidy(FENCE.sub("", response.get("text") or ""))
        except Exception:
            value = ""
        found = problems(value, facts)
        if not found:
            with _LOCK:
                _CACHE[key] = value
                while len(_CACHE) > _CACHE_SIZE:
                    _CACHE.popitem(last=False)
            return value
        messages = messages + [
            {"role": "model", "text": value or "(no answer)"},
            {
                "role": "user",
                "text": "Fix these problems and return the whole prompt again: "
                + "; ".join(found),
            },
        ]
    return None


def model_prompt(client, project, shot_id, draft):
    """The prompt our H3 machines read for this shot, or None to send the person's own."""
    shot = next((e for e in project["entities"] if e["id"] == shot_id), None)
    if shot is None:
        return None
    return rewrite(client, source(project, shot, draft))
