"""User feedback and friction signals for the product loop (#382).

Three sources land in one private append-only log:
- said:    the owner used /idea, rated a film, or Gemini noticed a request or
           complaint inside a normal message ("能加字幕吗");
- felt:    friction the bot observed (not understood, wrong part picked,
           reverted a fix, stale button, unsupported message, error);
- outcome: what happened (film delivered, fixes per film, satisfied).

Raw text stays in this private log (the code repository is public). The
digest tool turns it into anonymised themes. Rows carry an id and a status so
a shipped request can be answered back to whoever asked.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import time
import uuid

KINDS = {
    # said
    "idea", "rating", "rating_note", "model_request", "model_complaint", "model_praise",
    # felt
    "not_understood", "wrong_part", "reverted", "stale_button", "unsupported", "error",
    "generation_held",
    # outcome
    "film_delivered", "satisfied", "fix_applied", "new_film",
}  # fmt: skip


def person(chat_id):
    """Stable pseudonym for digests; the raw chat id stays only in the log."""
    return hashlib.sha256(f"inkseq-feedback:{chat_id}".encode()).hexdigest()[:10]


class FeedbackLog:
    def __init__(self, path, clock=time.time):
        self.path = Path(path)
        self.clock = clock

    def record(self, kind, chat_id, text="", **context):
        if kind not in KINDS:
            raise ValueError(f"unknown feedback kind {kind}")
        row = {
            "id": uuid.uuid4().hex[:12],
            "at": round(self.clock(), 3),
            "kind": kind,
            "chat": chat_id,
            "person": person(chat_id),
            "text": (text or "")[:1000],
            "status": "new",
            **{k: v for k, v in context.items() if v is not None},
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        except OSError:
            return None  # feedback must never break the conversation
        return row["id"]

    def rows(self, since=0.0):
        if not self.path.exists():
            return []
        rows = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if row.get("at", 0) >= since:
                rows.append(row)
        return rows

    def shipped_unannounced(self):
        """Shipped requests whose authors asked to be told and were not yet."""
        return [r for r in self.rows()
                if r.get("status") == "shipped" and r.get("notify") and not r.get("notified")]

    def mark(self, row_id, **changes):
        """Update one row in place (status, notify, theme)."""
        rows = self.rows()
        for row in rows:
            if row["id"] == row_id:
                row.update(changes)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
                       encoding="utf-8")  # fmt: skip
        tmp.replace(self.path)
