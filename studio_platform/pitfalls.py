"""One table of what we have learned about faults in generated shots (#1237).

``pitfalls.json`` holds one entry per lesson. Every place that uses such a lesson reads
it from here, so a lesson is written once and found by its id.

An entry:

- ``id``: ``stage.name``, lower case, unique. ``take.*`` names a fault a person sees in a
  finished take; other stages name a cause (``prompt.*``, ``cam.*``, ``cont.*`` ...).
- ``status``: ``proposed`` (believed, not measured), ``measured`` (measured, not switched
  on), ``in_use`` or ``retired`` (a re-test failed; the entry stays so nobody adds it again).
  Any status but ``proposed`` needs ``measured``: a line starting with the date, saying
  what was run, on which model, and the rate.
- ``uses``: where the entry acts.
  ``complaint``: when a person describes the fault, Inky answers with ``cause`` and ``fix``
  (``user_words``, ``cause``, ``fix``, ``assistant``). A person saw the fault, so an entry
  that is only ``proposed`` may still answer.
  ``prevent``: the shot text is rewritten before shooting (``match``, ``prevent``).
  ``check``: a finished frame or take is checked (``check``: ``stage``, ``how`` = code,
  judge or human, ``question``, ``expected``).
  ``prevent`` and ``check`` change or judge work nobody complained about, so they need
  ``in_use``. Nothing reads these two yet; they are held to the same rule from the start.
- ``models``: the models and versions the measurement was made on.
- ``match``: ``shot_text`` (regular expressions), ``film`` (a named rule over the whole
  story) and ``shot`` (a named rule over one shot).
- ``risk``, ``evidence``, ``retest_after``: free text for whoever maintains the entry.

``cause``, ``fix`` and ``assistant`` reach people through Inky: no model names, suppliers,
prices or internal ids there. A broken table stops the app at import.
"""

import json
import re
from pathlib import Path

PATH = Path(__file__).with_name("pitfalls.json")
STATUSES = ("proposed", "measured", "in_use", "retired")
USES = ("complaint", "prevent", "check")
HOW = ("code", "judge", "human")
FIELDS = {
    "id", "status", "uses", "models", "match", "risk", "cause", "prevent", "check",
    "user_words", "fix", "assistant", "evidence", "measured", "retest_after",
}  # fmt: skip
_ID = re.compile(r"^[a-z]+\.[a-z0-9_]+$")
_DATED = re.compile(r"^20\d\d-\d\d-\d\d \S")


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def problems(entry):
    """What is wrong with one entry, as short codes; empty when it is sound."""
    if not isinstance(entry, dict):
        return ["not_an_object"]
    found = [f"unknown_field:{key}" for key in sorted(set(entry) - FIELDS)]
    ident, status, uses = entry.get("id"), entry.get("status"), entry.get("uses")
    if not isinstance(ident, str) or not _ID.match(ident):
        found.append("id")
    if status not in STATUSES:
        found.append("status")
    if (
        not isinstance(uses, list)
        or set(uses) - set(USES)
        or len(set(uses)) != len(uses)
    ):
        found.append("uses")
        uses = []
    measured = entry.get("measured", "")
    if status != "proposed" and not (_text(measured) and _DATED.match(measured)):
        found.append("measured_needs_a_dated_line")
    if status == "retired" and uses:
        found.append("retired_entry_in_use")
    if "complaint" in uses:
        words = entry.get("user_words")
        if not isinstance(words, list) or not words or not all(map(_text, words)):
            found.append("user_words")
        found += [key for key in ("cause", "fix") if not _text(entry.get(key))]
        assistant = entry.get("assistant")
        if assistant != "point" and not (
            isinstance(assistant, str) and _text(assistant.removeprefix("edit: "))
            and assistant.startswith("edit: ")
        ):  # fmt: skip
            found.append("assistant")
    if {"prevent", "check"} & set(uses) and status != "in_use":
        found.append("unmeasured_entry_switched_on")
    if "prevent" in uses:
        found += [key for key in ("match", "prevent", "risk") if not entry.get(key)]
    if "check" in uses:
        check = entry.get("check")
        if (
            not isinstance(check, dict)
            or check.get("how") not in HOW
            or not all(
                _text(check.get(key)) for key in ("stage", "question", "expected")
            )
        ):
            found.append("check")
    match = entry.get("match", {})
    if not isinstance(match, dict) or set(match) - {"shot_text", "film", "shot"}:
        found.append("match")
    else:
        for pattern in match.get("shot_text", []):
            try:
                re.compile(pattern)
            except (re.error, TypeError):
                found.append("match_shot_text")
    return found


def load(path=PATH):
    table = json.loads(Path(path).read_text(encoding="utf-8"))
    entries = table.get("entries") if isinstance(table, dict) else None
    if not isinstance(entries, list):
        raise ValueError("pitfalls_invalid:entries")
    seen = set()
    for entry in entries:
        found = problems(entry)
        ident = entry.get("id") if isinstance(entry, dict) else None
        if ident in seen:
            found.append("duplicate_id")
        seen.add(ident)
        if found:
            raise ValueError(f"pitfalls_invalid:{ident}:{','.join(found)}")
    return tuple(entries)


ENTRIES = load()


def used_for(use):
    """The entries that act in one place, in the table's order."""
    return tuple(entry for entry in ENTRIES if use in entry["uses"])
