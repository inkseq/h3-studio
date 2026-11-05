"""What Inky compares a shot against: the story up to that shot, in reading order (#1073).

The judge refuses about 32,000 input tokens outright (measured, docs/EXPERIMENTS.md), and Chinese is
about one token per character. A long story keeps its nearest shots and says how many were left out.
"""

import json

from .inky_source import sources
from .repository import request_hash
from .story_chat import shots_of

REVISION = "inky-continuity-v1-20261106"
MAX_STATE_CHARS = 26000
MAX_CARD_CHARS = 400


def _line(number, value):
    parts = (
        f"#{number}",
        value["place"],
        ", ".join(value["characters"]),
        ", ".join(value["props"]),
        " ".join(value["shot_action"].split()),
    )
    return " | ".join(parts)


def _cards(project, value):
    wanted = (
        ("character", value["characters"]),
        ("location", [value["place"]] if value["place"] else []),
        ("prop", value["props"]),
    )
    result = {}
    for kind, names in wanted:
        for entity in project.get("entities", []):
            if entity.get("type") != kind or entity.get("title") not in names:
                continue
            text = entity.get("description")
            if isinstance(text, str) and text.strip():
                result[entity["title"]] = text.strip()[:MAX_CARD_CHARS]
    return result


def _state(target, lines, cards):
    shown = list(lines)
    while True:
        state = {
            "target_shot": target,
            "story_so_far": "\n".join(shown),
            "cards": cards,
            "earlier_shots_not_shown": len(lines) - len(shown),
        }
        if not shown or len(json.dumps(state, ensure_ascii=False)) <= MAX_STATE_CHARS:
            return state
        shown.pop(0)


def reviews(project):
    """Shot id to review, in reading order. Nothing to compare with means no review."""
    current = sources(project)
    result, earlier, numbers = {}, [], {}
    for number, shot in enumerate(shots_of(project, None), start=1):
        source = current.get(shot["id"])
        if source is None or not source["context"]["shot_action"].strip():
            continue
        value = source["context"]
        line = _line(number, value)
        if earlier:
            state = _state(line, earlier, _cards(project, value))
            result[shot["id"]] = {
                "source": source,
                "state": state,
                "digest": request_hash({"revision": REVISION, "state": state}),
                "numbers": dict(numbers),
                "others": {n: current[i]["source_id"] for n, i in numbers.items()},
            }
        earlier.append(line)
        numbers[number] = shot["id"]
    return result
