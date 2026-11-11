"""What Inky compares a shot against: the story up to that shot, in reading order (#1073).

The judge refuses about 32,000 input tokens outright (measured, docs/EXPERIMENTS.md), and Chinese is
about one token per character. A long story keeps its nearest shots and says how many were left out.
"""

import itertools
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


def _encoded(line):
    """Characters the line takes inside a JSON string."""
    return len(json.dumps(line, ensure_ascii=False)) - 2


def _state(target, lines, cards, ends=None):
    """The nearest earlier shots that fit, as dropping the oldest one at a time would leave them.

    One at a time cost a full encoding per line left out, for every shot, on
    every save and open of a long story, all inside the owner's content lock
    (#1192). Leaving out one more line always makes the state shorter, so there
    is one count where it starts to fit: `ends` (the running total of encoded
    line lengths) says where, and two real encodings confirm it.
    """
    count = len(lines)
    if ends is None:
        ends = list(itertools.accumulate(map(_encoded, lines), initial=0))

    def state(hidden):
        return {
            "target_shot": target,
            "story_so_far": "\n".join(lines[hidden:]),
            "cards": cards,
            "earlier_shots_not_shown": hidden,
        }

    def fits(hidden):
        return (
            hidden == count
            or len(json.dumps(state(hidden), ensure_ascii=False)) <= MAX_STATE_CHARS
        )

    rest = len(json.dumps(state(count), ensure_ascii=False)) - len(str(count))

    def expected(hidden):
        # Every line break between two lines is two characters once encoded.
        story = ends[count] - ends[hidden] + 2 * max(count - hidden - 1, 0)
        return rest + len(str(hidden)) + story

    low, high = 0, count
    while low < high:
        middle = (low + high) // 2
        if expected(middle) <= MAX_STATE_CHARS:
            high = middle
        else:
            low = middle + 1
    # The arithmetic only chooses where to look; the encoder decides.
    while not fits(low):
        low += 1
    while low and fits(low - 1):
        low -= 1
    return state(low)


def reviews(project, only=None):
    """Shot id to review, in reading order. Nothing to compare with means no review.

    With `only`, that shot's review alone is built; the others still count as earlier shots.
    """
    current = sources(project)
    result, earlier, ends, numbers = {}, [], [0], {}
    for number, shot in enumerate(shots_of(project, None), start=1):
        source = current.get(shot["id"])
        if source is None or not source["context"]["shot_action"].strip():
            continue
        value = source["context"]
        line = _line(number, value)
        if earlier and only in (None, shot["id"]):
            state = _state(line, earlier, _cards(project, value), ends)
            result[shot["id"]] = {
                "source": source,
                "state": state,
                "digest": request_hash({"revision": REVISION, "state": state}),
                "numbers": dict(numbers),
                "others": {n: current[i]["source_id"] for n, i in numbers.items()},
            }
        earlier.append(line)
        ends.append(ends[-1] + _encoded(line))
        numbers[number] = shot["id"]
    return result
