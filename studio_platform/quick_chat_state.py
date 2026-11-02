"""Single Quick Chat record/schema authority and shared request contracts."""

from __future__ import annotations

import base64
import json
import logging
from dataclasses import dataclass

from sqlalchemy import (
    JSON,
    Column,
    Float,
    Integer,
    MetaData,
    String,
    Table,
    UniqueConstraint,
)

from .project_validation import ID
from .repository import (
    canonical,
)

logger = logging.getLogger(__name__)


EMPTY_INPUTS = {
    "first_frame": None,
    "last_frame": None,
    "images": [],
    "videos": [],
    "audios": [],
    "guides": [],
}


DEFAULT_SETTINGS = {
    "recipe_id": "h3-base-fl2va-v1",
    "controls": {"duration": 5, "resolution": "480P"},
    "copies": 1,
}


metadata = MetaData()


objects = Table(
    "platform_quick_chat_objects",
    metadata,
    Column("id", String(80), primary_key=True),
    Column("tenant", String(200), nullable=False),
    Column("owner", String(200), nullable=False),
    Column("session_id", String(80), nullable=False),
    Column("kind", String(40), nullable=False),
    Column("parent_id", String(80)),
    Column("business_key", String(200)),
    Column("version", Integer, nullable=False),
    Column("payload", JSON, nullable=False),
    Column("created_at", Float, nullable=False),
    Column("updated_at", Float, nullable=False),
    UniqueConstraint("tenant", "owner", "kind", "business_key"),
)


operations = Table(
    "platform_quick_chat_operations",
    metadata,
    Column("tenant", String(200), primary_key=True),
    Column("owner", String(200), primary_key=True),
    Column("namespace", String(200), primary_key=True),
    Column("key", String(160), primary_key=True),
    Column("fingerprint", String(64), nullable=False),
    Column("object_id", String(80), nullable=False),
    Column("created_at", Float, nullable=False),
)


events = Table(
    "platform_quick_chat_events",
    metadata,
    Column("id", String(80), primary_key=True),
    Column("tenant", String(200), nullable=False),
    Column("owner", String(200), nullable=False),
    Column("session_id", String(80), nullable=False),
    Column("seq", Integer, nullable=False),
    Column("type", String(60), nullable=False),
    Column("resource_id", String(80), nullable=False),
    Column("actor_id", String(200), nullable=False),
    Column("created_at", Float, nullable=False),
    UniqueConstraint("tenant", "owner", "session_id", "seq"),
)


class QuickChatError(Exception):
    def __init__(
        self, code, message, status=409, *, retryable=False, field_errors=None
    ):
        self.code, self.message, self.status, self.retryable = (
            code,
            message,
            status,
            retryable,
        )
        self.field_errors = field_errors
        super().__init__(code)


@dataclass
class QuickChatHooks:
    preflight: object
    create_planned: object
    enqueue: object
    public_job: object
    cancel: object
    refresh_planned: object = None


def fields(body, allowed, required=()):
    if (
        not isinstance(body, dict)
        or set(body) - set(allowed)
        or not set(required) <= set(body)
    ):
        raise QuickChatError("invalid_request", "请求字段不符合创作契约。", 422)
    return canonical(body)


def key_check(key):
    if not isinstance(key, str) or not ID.fullmatch(key):
        raise QuickChatError(
            "invalid_idempotency_key", "请提供稳定的1–160字符Idempotency-Key。", 422
        )


def cursor_encode(principal, session_id, boundary, direction):
    value = {
        "owner": principal.owner,
        "session": session_id,
        "boundary": boundary,
        "direction": direction,
    }
    return (
        base64.urlsafe_b64encode(json.dumps(value, separators=(",", ":")).encode())
        .decode()
        .rstrip("=")
    )


def cursor_decode(value, principal, session_id, direction):
    try:
        if not isinstance(value, str) or len(value) > 1024:
            raise ValueError()
        obj = json.loads(base64.urlsafe_b64decode(value + "=" * ((-len(value)) % 4)))
        if (
            set(obj) != {"owner", "session", "boundary", "direction"}
            or obj["owner"] != principal.owner
            or obj["session"] != session_id
            or obj["direction"] != direction
        ):
            raise ValueError()
        return obj["boundary"]
    except Exception:
        logger.info("quick_chat_cursor_rejected")
        raise QuickChatError(
            "invalid_cursor", "分页位置无效，请重新加载记录。", 422
        ) from None
