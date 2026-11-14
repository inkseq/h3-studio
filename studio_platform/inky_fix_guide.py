"""Recorded fixes for complaints about a finished take (#1227).

The rows are the entries of the pitfalls table (``pitfalls.json``, #1237) that answer a
complaint: ``user_words`` (phrases a person types, Chinese and English), ``cause``, ``fix``
and ``assistant`` (``point`` when the person fixes it elsewhere in the app, or
``edit: ...`` for a card change Inky can offer). They are user facing: no model names,
suppliers, prices or internal ids.

A typed message picks at most three rows by its phrases, longest matches first, and only
once the story has a finished take. No model call; the rows ride on the turn's note.
"""

import re

from . import pitfalls
from .prompt_files import text as prompt_text

LIMIT = 3
NOTE = prompt_text("inky_fix_note")


def _pattern(phrase):
    # English phrases match whole words ("error" not in "terror"); Chinese has no spaces.
    escaped = re.escape(phrase)
    if re.match(r"[a-z]", phrase):
        escaped = r"(?<![a-z])" + escaped
    if re.search(r"[a-z]$", phrase):
        escaped += r"(?![a-z])"
    return re.compile(escaped)


def _row(entry):
    assistant = entry["assistant"]
    return {
        "id": entry["id"],
        "when": tuple(
            (phrase, _pattern(phrase))
            for phrase in (word.strip().lower() for word in entry["user_words"])
        ),
        "cause": entry["cause"],
        "fix": entry["fix"],
        "edit": None if assistant == "point" else assistant.removeprefix("edit: "),
    }


_built = (None, ())


def rows():
    """The answering rows of the table in force; rebuilt only when the table changes."""
    global _built
    table = pitfalls.entries()
    if _built[0] is not table:
        _built = (table, tuple(_row(e) for e in table if "complaint" in e["uses"]))
    return _built[1]


def match(text, limit=LIMIT):
    """Rows whose phrases appear in the message, the longest total match first."""
    lowered, table = text.lower(), rows()
    scored = []
    for index, row in enumerate(table):
        score = sum(len(p) for p, pattern in row["when"] if pattern.search(lowered))
        if score:
            scored.append((-score, index))
    return [table[index] for _score, index in sorted(scored)[:limit]]


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
