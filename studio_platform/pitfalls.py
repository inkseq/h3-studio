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
  ``prevent``: advice before shooting when the shot text matches (``match``, ``prevent``,
  and optionally ``ask``: a request the person can send to Inky, ``{number}`` being the
  shot). ``prevent`` and ``ask`` are one text, or ``{"zh": ..., "en": ...}``.
  ``check``: a finished frame or take is checked (``check``: ``stage``, ``how`` = code,
  judge or human, ``question``, ``expected``).
  ``prevent`` and ``check`` change or judge work nobody complained about, so they need
  ``in_use``. Nothing reads these two yet; they are held to the same rule from the start.
- ``models``: the models and versions the measurement was made on.
- ``match``: ``shot_text`` (regular expressions, any one hits), ``with`` (one of these
  must also hit) and ``unless`` (none of these may), ``film`` (a named rule over the
  whole story) and ``shot`` (a named rule over one shot).
- ``risk``, ``evidence``, ``retest_after``: free text for whoever maintains the entry.

``cause``, ``fix``, ``assistant``, ``prevent`` and ``ask`` reach people through Inky: no
model names, suppliers, prices or internal ids there. A broken table stops the app at
import.

Two files, one table. ``pitfalls.json`` ships with the code, and this repository is public,
so it holds only what may be public. Measured lessons live in a private file in the same
format, installed on the host as ``<data dir>/runtime-secrets/pitfalls.json`` (root-owned,
like the other files there). ``entries()`` is the public table with the private entries
laid over it by id; the private file is read again when it changes, so an update needs no
restart. A private file that is missing, unreadable or unsound leaves the public table in
force and logs one warning: bad private content must not stop the app.

``python -m studio_platform.pitfalls check <file>`` checks a private file before it is
installed (``-`` reads standard input).
"""

import json
import logging
import re
import threading
import time
from pathlib import Path

from .protected_config import read_protected_json

LOG = logging.getLogger(__name__)
PATH = Path(__file__).with_name("pitfalls.json")
PRIVATE_NAME = "pitfalls.json"
PRIVATE_LIMIT = 2 * 1024 * 1024
RECHECK_SECONDS = 30
# Names people must never see (owner: no models, suppliers, prices or internal steps).
INTERNAL = re.compile(
    r"H3|Pika|EveryFrame|Vivix|Modal|RunPod|Gemini|Terra|Jev|Eleven|Seedance|Kling|"
    r"\bL20\d{6}|USD|\$|\.py\b|ASR|识别时间戳|omni",
    re.I,
)
STATUSES = ("proposed", "measured", "in_use", "retired")
USES = ("complaint", "prevent", "check")
HOW = ("code", "judge", "human")
FIELDS = {
    "id", "status", "uses", "models", "match", "risk", "cause", "prevent", "check",
    "user_words", "fix", "assistant", "evidence", "measured", "retest_after", "ask",
}  # fmt: skip
_ID = re.compile(r"^[a-z]+\.[a-z0-9_]+$")
_DATED = re.compile(r"^20\d\d-\d\d-\d\d \S")


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def _said(value):
    """One text, or the same text in Chinese and English."""
    if isinstance(value, dict):
        return set(value) == {"zh", "en"} and all(map(_text, value.values()))
    return _text(value)


def in_language(value, language):
    """A ``prevent`` or ``ask`` text for a shot written in this language."""
    if isinstance(value, dict):
        return value["zh" if language == "zh" else "en"]
    return value


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
        said = " ".join(
            str(entry.get(key, "")) for key in ("cause", "fix", "assistant")
        )
        if INTERNAL.search(said):
            found.append("names_internals")
    if {"prevent", "check"} & set(uses) and status != "in_use":
        found.append("unmeasured_entry_switched_on")
    if "prevent" in uses:
        found += [key for key in ("match", "risk") if not entry.get(key)]
        if not _said(entry.get("prevent")):
            found.append("prevent")
        shown = json.dumps([entry.get("prevent"), entry.get("ask")], ensure_ascii=False)
        if INTERNAL.search(shown):
            found.append("names_internals")
    if "ask" in entry and not _said(entry["ask"]):
        found.append("ask")
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
    keys = {"shot_text", "with", "unless", "film", "shot"}
    if not isinstance(match, dict) or set(match) - keys:
        found.append("match")
    else:
        for key in ("shot_text", "with", "unless"):
            patterns = match.get(key, [])
            if not isinstance(patterns, list):
                found.append(f"match_{key}")
                continue
            for pattern in patterns:
                try:
                    re.compile(pattern)
                except (re.error, TypeError):
                    found.append(f"match_{key}")
    return found


def _checked(table):
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


def load(path=PATH):
    return _checked(json.loads(Path(path).read_text(encoding="utf-8")))


BASE = load()


class _PrivateError(Exception):
    pass


def _read_private(path):
    """The private file's content; the same ownership and mode rules as a credential."""
    return read_protected_json(path, _PrivateError, limit=PRIVATE_LIMIT)


_lock = threading.Lock()
_private_path = None
_seen = None  # (mtime_ns, size) of the private file last read, or "absent"
_checked_at = 0.0
_current = BASE
_state = "absent"


def configure(settings):
    """Point this process at its private file (once per app); None means public only."""
    global _private_path, _seen, _checked_at, _current, _state
    with _lock:
        _private_path = (
            None
            if settings is None
            else Path(settings.data_dir) / "runtime-secrets" / PRIVATE_NAME
        )
        _seen, _checked_at, _current, _state = None, 0.0, BASE, "absent"


def _merged(private):
    by_id = {entry["id"]: entry for entry in private}
    merged = [by_id.pop(entry["id"], entry) for entry in BASE]
    return tuple(merged + list(by_id.values()))


def _refresh():
    global _seen, _current, _state
    try:
        info = _private_path.stat()
        mark = (info.st_mtime_ns, info.st_size)
    except OSError:
        mark = "absent"
    if mark == _seen:
        return
    _seen = mark
    if mark == "absent":
        _current, _state = BASE, "absent"
        return
    try:
        private = _checked(_read_private(_private_path))
    except (_PrivateError, ValueError) as error:
        # Never the content: only which rule the file broke.
        reason = str(error) if isinstance(error, ValueError) else "unreadable"
        LOG.warning("pitfalls_private_invalid reason=%s", reason[:120])
        _current, _state = BASE, "invalid"
        return
    _current, _state = _merged(private), "loaded"
    LOG.info("pitfalls_private_loaded entries=%s total=%s", len(private), len(_current))


def entries():
    """The table in force: public entries with the private ones laid over them by id."""
    global _checked_at
    if _private_path is None:
        return BASE
    with _lock:
        now = time.monotonic()
        if _seen is None or now - _checked_at >= RECHECK_SECONDS:
            _checked_at = now
            _refresh()
        return _current


def state():
    """absent, loaded or invalid: what became of the private file at the last look."""
    entries()
    return _state


def used_for(use):
    """The entries that act in one place, in the table's order."""
    return tuple(entry for entry in entries() if use in entry["uses"])


def main(argv):
    import sys

    if len(argv) != 2 or argv[0] != "check":
        print("usage: python -m studio_platform.pitfalls check <file or ->")
        return 2
    raw = sys.stdin.read() if argv[1] == "-" else Path(argv[1]).read_text("utf-8")
    try:
        checked = _checked(json.loads(raw))
    except ValueError as error:
        print(f"invalid: {str(error)[:300]}")
        return 1
    counts = {use: sum(use in entry["uses"] for entry in checked) for use in USES}
    print(f"ok: {len(checked)} entries, {counts}")
    return 0


if __name__ == "__main__":
    import sys

    raise SystemExit(main(sys.argv[1:]))
