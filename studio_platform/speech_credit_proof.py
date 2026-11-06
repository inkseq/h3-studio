"""Frozen voice quotes and original audio attachment evidence; no provider calls."""

import json
import re

from sqlalchemy import inspect, select

from .assets import asset_table
from .content_lifecycle import content_is_deleted
from .credits import CreditError
from .guided import receipts
from .repository import documents, request_hash

SPEECH_FIELDS = frozenset(
    {
        "voice_id",
        "voice_revision",
        "direction",
        "language",
        "text_sha256",
        "characters",
    }
)
# Receipts written before #1097 named the vendor instead of our voice revision.
# They are read only to recover their original hold; new bindings never use it.
LEGACY_SPEECH_FIELDS = SPEECH_FIELDS - {"voice_revision"} | {
    "provider",
    "model",
    "provider_voice",
}
PRICE_REVISION = "speech-characters-v1"
COMMON_FIELDS = frozenset(
    {"tenant", "owner", "project_id", "card_id", "request_id", "input_hash", "actor_id"}
)


def quote(characters):
    """Price the normalized spoken text, never provider-injected direction tags."""
    if type(characters) is not int or not 1 <= characters <= 500:
        raise CreditError("credit_service_binding_invalid", 422)
    return {"credits": 2 * ((characters + 99) // 100), "price_revision": PRICE_REVISION}


def validate_binding(binding):
    if type(binding) is not dict or set(binding) != COMMON_FIELDS | {"speech"}:
        raise CreditError("credit_service_binding_invalid", 422)
    for key in COMMON_FIELDS:
        value = binding[key]
        if (
            type(value) is not str
            or not 1 <= len(value) <= 200
            or value != value.strip()
            or any(ord(c) < 32 or ord(c) == 127 for c in value)
        ):
            raise CreditError("credit_service_binding_invalid", 422)
    speech = binding.get("speech")
    if type(speech) is not dict or set(speech) not in (
        SPEECH_FIELDS,
        LEGACY_SPEECH_FIELDS,
    ):
        raise CreditError("credit_service_binding_invalid", 422)
    for key in set(speech) - {"characters"}:
        value = speech[key]
        if (
            type(value) is not str
            or not 1 <= len(value) <= 200
            or value != value.strip()
            or any(ord(c) < 32 or ord(c) == 127 for c in value)
        ):
            raise CreditError("credit_service_binding_invalid", 422)
    quote(speech["characters"])
    if (
        not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", speech["voice_id"])
        or not (
            re.fullmatch(r"r[0-9]{1,4}", speech["voice_revision"])
            if "voice_revision" in speech
            else re.fullmatch(r"[a-z0-9_-]{1,40}", speech["provider"])
        )
        or speech["direction"] not in {"calm", "angry", "sad", "happy"}
        or speech["language"] not in {"zh", "en"}
        or not re.fullmatch(r"[0-9a-f]{64}", speech["text_sha256"])
        or not re.fullmatch(r"[A-Za-z0-9_-]{8,64}", binding["request_id"])
        or binding["card_id"] != "speech-" + binding["request_id"]
        or binding["input_hash"] != request_hash(speech)
    ):
        raise CreditError("credit_service_binding_invalid", 422)
    return {**binding, "speech": dict(speech)}


def is_legacy(binding):
    return "voice_revision" not in binding["speech"]


def binding(scope, reference_id, value):
    """Validate the same frozen request for free execution and paid admission."""
    value = validate_binding(value)
    if reference_id != "speech:" + value["request_id"]:
        raise CreditError("credit_service_binding_invalid", 422)
    if (value["tenant"], value["owner"], value["project_id"], value["actor_id"]) != (
        scope.tenant_id,
        scope.owner_id,
        scope.project_id,
        scope.actor_id,
    ):
        raise CreditError("credit_idempotency_conflict")
    return value


def _need(condition):
    if not condition:
        raise CreditError("credit_attachment_outcome_unconfirmed")


def _entities(project, ident):
    items = project.get("entities")
    _need(type(items) is list and all(type(item) is dict for item in items))
    return [item for item in items if item.get("id") == ident]


def _owned_asset(connection, binding, asset_id):
    _need(type(asset_id) is str and re.fullmatch(r"[A-Za-z0-9_-]{1,200}", asset_id))
    asset = (
        connection.execute(
            select(asset_table).where(
                asset_table.c.id == asset_id,
                asset_table.c.tenant == binding["tenant"],
                asset_table.c.owner == binding["owner"],
                asset_table.c.project_id == binding["project_id"],
                asset_table.c.status == "ready",
            )
        )
        .mappings()
        .first()
    )
    try:
        record = json.loads(asset["record"]) if asset else None
    except (TypeError, ValueError):
        record = None
    _need(
        type(record) is dict
        and record.get("id") == asset_id
        and record.get("project_id") == binding["project_id"]
        and record.get("client_asset_id") == "speech:" + binding["request_id"]
        and record.get("kind") == "audio"
        and record.get("status") == "ready"
        and not content_is_deleted(
            connection, binding["tenant"], binding["owner"], asset_id=asset_id
        )
    )


def attachment(connection, binding, *, release=False):
    """Only an immutable attachment edit plus its owned asset proves delivery.

    Current project edits cannot erase accepted delivery or forge server delivery.
    Only the protected receipt, committed atomically with attachment, is evidence.
    """
    binding = validate_binding(binding)
    current = connection.execute(
        select(documents.c.payload)
        .where(
            documents.c.tenant_id == binding["tenant"],
            documents.c.owner_id == binding["owner"],
            documents.c.project_id == "__projects",
            documents.c.kind == "project",
            documents.c.document_id == binding["project_id"],
        )
        .with_for_update()
    ).scalar_one_or_none()
    _need(type(current) is dict and inspect(connection).has_table(receipts.name))
    committed = connection.execute(
        select(receipts.c.response).where(
            receipts.c.tenant == binding["tenant"],
            receipts.c.owner == binding["owner"],
            receipts.c.actor == binding["actor_id"],
            receipts.c.project_id == binding["project_id"],
            receipts.c.key == "speech-v1:attach:" + binding["request_id"],
        )
    ).scalar_one_or_none()
    if release and committed is not None:
        raise CreditError("credit_attachment_exists")
    if committed is None:
        return None
    _need(
        type(committed) is dict
        and committed.get("id") == binding["project_id"]
        and type(committed.get("version")) is int
        and committed["version"] > 0
        and type(committed.get("project")) is dict
    )
    entities = _entities(committed["project"], binding["card_id"])
    _need(len(entities) == 1 and entities[0].get("type") == "audio")
    data = entities[0].get("data")
    _need(type(data) is dict)
    speech = data.get("speech")
    fields = set(binding["speech"])
    _need(
        type(speech) is dict
        and set(speech) == fields | {"request_id", "input_hash", "provider_characters"}
        and all(
            type(speech[field]) is type(binding["speech"][field])
            and speech[field] == binding["speech"][field]
            for field in fields
        )
        and speech["request_id"] == binding["request_id"]
        and speech["input_hash"] == binding["input_hash"]
        and type(speech["provider_characters"]) is int
        and speech["provider_characters"] >= 0
    )
    asset_id = data.get("cloudAssetId")
    _owned_asset(connection, binding, asset_id)
    content_path = f"/v1/assets/{asset_id}/content"
    _need(data.get("cloudContentPath") == content_path)
    return {
        "entity_id": binding["card_id"],
        "asset_id": asset_id,
        "content_path": content_path,
        "voice_id": binding["speech"]["voice_id"],
    }
