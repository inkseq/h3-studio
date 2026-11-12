"""Recorded fixes for complaints about a finished take (#1227).

The rows live in ``prompts/inky_fix_guide.md``: one ``## name`` part per complaint with
``when`` (phrases a person types, Chinese and English, split by ``|``), ``cause``, ``fix``
and ``inky`` (``point`` when the person fixes it elsewhere in the app, or ``edit: ...``
for a card change Inky can offer). They are curated from the film team's fix guide and
are user facing: no model names, suppliers, prices or internal ids.

A typed message picks at most three rows by its phrases, longest matches first, and only
once the story has a finished take. No model call; the rows ride on the turn's note.
"""

import re

from .prompt_files import sections
from .prompt_files import text as prompt_text

LIMIT = 3
NOTE = prompt_text("inky_fix_note")
KEYS = ("when", "cause", "fix", "inky")


def _pattern(phrase):
    # English phrases match whole words ("error" not in "terror"); Chinese has no spaces.
    escaped = re.escape(phrase)
    if re.match(r"[a-z]", phrase):
        escaped = r"(?<![a-z])" + escaped
    if re.search(r"[a-z]$", phrase):
        escaped += r"(?![a-z])"
    return re.compile(escaped)


def _row(name, body):
    fields = {}
    for line in body.splitlines():
        if not line.strip():
            continue
        key, sep, value = line.partition(": ")
        if not sep or key not in KEYS or key in fields or not value.strip():
            raise ValueError(f"inky_fix_guide_invalid:{name}")
        fields[key] = value.strip()
    when = [p.strip().lower() for p in fields.get("when", "").split("|") if p.strip()]
    inky = fields.get("inky", "")
    if set(fields) != set(KEYS) or not when:
        raise ValueError(f"inky_fix_guide_invalid:{name}")
    if inky != "point" and not inky.startswith("edit: "):
        raise ValueError(f"inky_fix_guide_invalid:{name}")
    return {
        "id": name,
        "when": tuple((phrase, _pattern(phrase)) for phrase in when),
        "cause": fields["cause"],
        "fix": fields["fix"],
        "edit": inky.removeprefix("edit: ") if inky != "point" else None,
    }


ROWS = tuple(_row(name, body) for name, body in sections("inky_fix_guide").items())


def match(text, limit=LIMIT):
    """Rows whose phrases appear in the message, the longest total match first."""
    lowered = text.lower()
    scored = []
    for index, row in enumerate(ROWS):
        score = sum(len(p) for p, pattern in row["when"] if pattern.search(lowered))
        if score:
            scored.append((-score, index))
    return [ROWS[index] for _score, index in sorted(scored)[:limit]]


def has_take(cards):
    """A complaint about a take only makes sense once a shot has a finished video."""
    return any(s["selected_artifact_id"] or s["takes"] for s in cards["shots"])


def note(rows):
    if not rows:
        return ""
    lines = [
        f"- {row['cause']} Fix: {row['fix']} "
        + (
            f"You can edit: {row['edit']}"
            if row["edit"]
            else "The person does it; you change no card for this."
        )
        for row in rows
    ]
    return NOTE.format(notes="\n".join(lines)) + "\n\n"
