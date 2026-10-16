"""Optional one-shot history naming on the existing owner-isolated session row.

The first accepted turn/card is the input authority. Background work claims its
one supplier call durably before leaving SQL; recovery never repeats that call.
"""
from __future__ import annotations

import copy
import re
import unicodedata
import uuid

from sqlalchemy import select

from .auth import Principal

TITLE_MODEL = "gemma-4-31b-it"
DEFAULT_TITLE = "新的创作"
TITLE_INPUT_CHARS = 2000
TITLE_MAX_CHARS = 40


def clean_title(value):
    if isinstance(value, dict):
        value = value.get("text")
    if not isinstance(value, str):
        raise ValueError("invalid_title_response")
    # Never preserve model markup, hidden control characters or a long reply.
    lines = [line.strip() for line in value.splitlines() if line.strip()]
    if len(lines) != 1:
        raise ValueError("invalid_title_response")
    title = lines[0].strip("#*`\"'“”‘’ ")
    title = re.sub(r"^(?:标题|名称|Title)\s*[:：]\s*", "", title, flags=re.IGNORECASE)
    title = " ".join("".join(c for c in title if not unicodedata.category(c).startswith("C")).split())
    if (not title or len(title) > TITLE_MAX_CHARS or not any(c.isalnum() for c in title)
            or any(c in title for c in "<>")):
        raise ValueError("invalid_title_response")
    return title


def public_title_state(payload):
    value = payload.get("title_generation")
    if value is None:
        return None
    return {k: value.get(k) for k in ("status", "model_id", "error_code")}


def title_error_code(error):
    # Exception messages/codes are untrusted and may contain prompts or keys.
    code = getattr(error, "code", None)
    if not isinstance(code, str):
        code = None
    if isinstance(error, TimeoutError) or code in {"upstream_timeout", "connection_failed"}:
        return "title_call_unknown"
    if code in {"profile_mismatch", "google_http_401", "google_http_403"}:
        return "title_unavailable"
    if code == "google_http_429":
        return "title_rate_limited"
    if isinstance(error, ValueError) or code in {"invalid_response", "empty_or_blocked"}:
        return "title_invalid_response"
    return "title_generation_failed"


class QuickChatTitleMixin:
    def _initial_title_state(self, *, title_supplied):
        if self.title_generator is None:
            return None
        return {"status": "manual" if title_supplied else "awaiting_input",
                "model_id": TITLE_MODEL, "error_code": None}

    def _queue_title(self, conn, principal, session, source):
        state = session["payload"].get("title_generation")
        text = source["payload"].get("text" if source["kind"] == "turn" else "prompt", "")
        if (self.title_generator is None or not state or state["status"] != "awaiting_input"
                or not any(c.isalnum() for c in text)):
            return
        payload = copy.deepcopy(session["payload"])
        payload["title_generation"] = {**state, "status": "pending", "claim_id": uuid.uuid4().hex,
            "source_id": source["id"], "source_kind": source["kind"], "queued_at": self.repo.clock(),
            "original_title": payload["title"]}
        self._put(conn, session, payload)

    def generate_title(self, principal, session_id):
        """Run at most one supplier request, outside SQL and after the write response.

        Only a write principal may trigger work. GET/list and replayed completed
        writes never start another call; a durable pending claim is safe to claim
        once when a response was lost before background work began.
        """
        if self.title_generator is None:
            return False
        try:
            with self.repo.transaction() as conn:
                session = self._access(principal, session_id, "projects:read", "projects:write", conn=conn, lock=True)
                state = session["payload"].get("title_generation")
                if self.title_generator is None or not state or state["status"] != "pending":
                    return False
                from .spending_limits import unquoted_spend_blocked
                if unquoted_spend_blocked(conn, self.tenant, principal):
                    payload = copy.deepcopy(session["payload"])
                    payload["title_generation"].update(status="failed", error_code="assistant_quote_unavailable", completed_at=self.repo.clock())
                    self._put(conn, session, payload)
                    return False
                source = self._get(conn, principal, state["source_id"], session_id, state["source_kind"])
                text = source["payload"]["text" if state["source_kind"] == "turn" else "prompt"]
                payload = copy.deepcopy(session["payload"])
                payload["title_generation"].update(status="running", started_at=self.repo.clock())
                self._put(conn, session, payload)
                claim_id = state["claim_id"]
            try:
                title = clean_title(self.title_generator.generate(text.strip()[:TITLE_INPUT_CHARS]))
                error_code = None
            except Exception as error:
                title, error_code = None, title_error_code(error)
            with self.repo.transaction() as conn:
                session = self._access(principal, session_id, "projects:read", "projects:write", conn=conn, lock=True)
                current = session["payload"].get("title_generation")
                if (not current or current["status"] != "running" or current.get("claim_id") != claim_id
                        or session["payload"]["title"] != current["original_title"]):
                    return False
                payload = copy.deepcopy(session["payload"])
                payload["title_generation"].update(status="failed" if error_code else "completed",
                    error_code=error_code, completed_at=self.repo.clock())
                if title is not None:
                    payload["title"] = title
                # Naming is metadata: preserve the current author's version and
                # settings. Explicit PATCH(title) remains versioned authoring.
                self._put(conn, session, payload)
                self._event(conn, principal, session, "session.title_failed" if error_code else "session.title_generated", session_id)
            return error_code is None
        except Exception:
            # Background DB/auth failures must not surface as failed card/video
            # writes. A claimed call remains fenced for SQL-only recovery.
            return False

    def recover_title_runs(self, *, older_than_s=180):
        """Fence stale one-shot work without sending text to any provider."""
        if older_than_s < 180:
            raise ValueError("invalid_title_recovery_window")
        from .quick_chat import objects
        cutoff = self.repo.clock()-older_than_s
        with self.repo.transaction() as conn:
            rows = [dict(r) for r in conn.execute(select(objects).where(objects.c.tenant == self.tenant,
                objects.c.kind == "session", objects.c.payload["title_generation"]["status"].as_string().in_(["pending", "running"]),
                objects.c.payload["title_generation"]["queued_at"].as_float() < cutoff)
                .order_by(objects.c.owner, objects.c.id).limit(100)).mappings()]
            recovered = 0
            for row in rows:
                principal = Principal(row["owner"], "quick-chat-title-recovery")
                current = self._get(conn, principal, row["id"], row["id"], "session", lock=True)
                state = current["payload"].get("title_generation")
                if not state or state["status"] not in {"pending", "running"}:
                    continue
                timestamp = state.get("started_at", state["queued_at"])
                if timestamp >= cutoff:
                    continue
                payload = copy.deepcopy(current["payload"])
                payload["title_generation"].update(status="failed", completed_at=self.repo.clock(),
                    error_code="title_call_unknown" if state["status"] == "running" else "title_interrupted_before_call")
                self._put(conn, current, payload)
                self._event(conn, principal, current, "session.title_failed", current["id"])
                recovered += 1
            return recovered
