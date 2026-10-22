"""Caller-transaction image charges using the existing customer-credit ledger.

The route owns provider execution and fences late attachment before releasing a
stale request. These primitives never call a provider, retry, or create jobs.
"""

import json
import re
from datetime import datetime

from sqlalchemy import inspect, select

from . import credits, credits_pricing
from .assets import asset_table
from .content_lifecycle import content_is_deleted, owner_reference_gate
from .credit_schema import entries
from .guided import receipts as edit_receipts
from .identity import lock_account_gate
from .repository import Conflict, documents, request_hash
from .spending_limits import (
    check_credit_spending,
    lock_owner,
    read_settings,
    require_credit_limits_reviewed,
)

REFERENCE_TYPE = "story_chat_image"
RELEASE_REASONS = frozenset(
    {
        "provider_failed",
        "provider_timeout_possible_output",
        "card_missing",
        "version_conflict",
        "store_failed",
        "stale_unattached",
    }
)
_BINDING_KEYS = {
    "tenant",
    "owner",
    "project_id",
    "card_id",
    "request_id",
    "input_hash",
    "actor_id",
}


def _binding(scope, reference_id, binding):
    if type(binding) is not dict or set(binding) != _BINDING_KEYS:
        raise credits.CreditError("credit_service_binding_invalid", 422)
    for value in binding.values():
        credits._text(value, 200)
    if (
        not re.fullmatch(r"[A-Za-z0-9_-]{8,80}", binding["request_id"])
        or not re.fullmatch(r"[0-9a-f]{64}", binding["input_hash"])
        or reference_id != "story_chat_image:" + binding["request_id"]
    ):
        raise credits.CreditError("credit_service_binding_invalid", 422)
    if (
        binding["tenant"],
        binding["owner"],
        binding["project_id"],
        binding["actor_id"],
    ) != (scope.tenant_id, scope.owner_id, scope.project_id, scope.actor_id):
        raise credits.CreditError("credit_idempotency_conflict")
    return dict(binding)


def _quote(value):
    if (
        type(value) is not dict
        or set(value) != {"credits", "price_revision"}
        or not isinstance(value["price_revision"], str)
        or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,64}", value["price_revision"])
    ):
        raise credits.CreditError("credit_quote_required")
    credits._integer(value["credits"], positive=True)
    return dict(value)


def _fingerprint(binding, quote):
    return request_hash({"binding": binding, "quote": quote})


def _context(connection, scope, reference_id, binding):
    binding = _binding(scope, reference_id, binding)
    # Same account-authority and content fences as normal writes. The spending
    # owner lock precedes BOTH daily projection and the credit account CAS.
    lock_account_gate(connection)
    owner_reference_gate(connection, scope.tenant_id, scope.owner_id)
    lock_owner(connection, scope.tenant_id, scope.owner_id)
    if not credits._ready(connection):
        return binding, None, None, None
    user = credits._user(connection, scope.tenant_id, scope.owner_id)
    rows = list(
        connection.execute(
            select(entries).where(
                entries.c.tenant == scope.tenant_id,
                entries.c.user_id == user["id"],
                entries.c.reference_type == REFERENCE_TYPE,
                entries.c.reference_id == reference_id,
            )
        ).mappings()
    )
    holds = [row for row in rows if row["kind"] == "hold"]
    terminals = [row for row in rows if row["kind"] in {"capture", "release"}]
    if len(holds) > 1 or len(terminals) > 1 or (terminals and not holds):
        raise credits.CreditError("credit_hold_conflict")
    hold = dict(holds[0]) if holds else None
    terminal = dict(terminals[0]) if terminals else None
    if hold:
        frozen = _quote(
            {"credits": hold["held_delta"], "price_revision": hold["price_revision"]}
        )
        if (
            hold["fingerprint"] != _fingerprint(binding, frozen)
            or hold["actor"] != binding["actor_id"]
            or hold["origin"] != binding["actor_id"]
            or hold["amount"] != 0
            or hold["idempotency_key"] != reference_id + ":hold"
        ):
            raise credits.CreditError("credit_idempotency_conflict")
        if terminal and (
            terminal["amount"]
            != (-hold["held_delta"] if terminal["kind"] == "capture" else 0)
            or terminal["held_delta"] != -hold["held_delta"]
            or terminal["actor"] != hold["actor"]
            or terminal["origin"] != hold["origin"]
            or terminal["price_revision"] != hold["price_revision"]
            or terminal["idempotency_key"] != reference_id + ":" + terminal["kind"]
            or terminal["fingerprint"]
            != request_hash({"hold": hold["id"], "kind": terminal["kind"]})
        ):
            raise credits.CreditError("credit_hold_conflict")
    return binding, user, hold, terminal


def _project(connection, binding):
    project = connection.execute(
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
    if type(project) is not dict:
        raise credits.CreditError("credit_attachment_outcome_unconfirmed")
    return project


def _attachment(connection, binding, *, release=False):
    current = _project(connection, binding)
    journey = current.get("journey", {})
    story = journey.get("storyChat", {}) if type(journey) is dict else None
    images = story.get("images", {}) if type(story) is dict else None
    if type(images) is not dict:
        raise credits.CreditError("credit_attachment_outcome_unconfirmed")
    # Charged attachment must use this key in the same Guided.mutate that
    # attaches the asset and writes journey.storyChat.images. Its immutable
    # response survives a later full-project PUT removing the mutable receipt.
    if not inspect(connection).has_table(edit_receipts.name):
        raise credits.CreditError("credit_attachment_outcome_unconfirmed")
    committed = connection.execute(
        select(edit_receipts.c.response).where(
            edit_receipts.c.tenant == binding["tenant"],
            edit_receipts.c.owner == binding["owner"],
            edit_receipts.c.actor == binding["actor_id"],
            edit_receipts.c.project_id == binding["project_id"],
            edit_receipts.c.key == "story-chat-image-" + binding["request_id"],
        )
    ).scalar_one_or_none()
    if release and (committed is not None or binding["request_id"] in images):
        raise credits.CreditError("credit_attachment_exists")
    if committed is None:
        if binding["request_id"] in images:
            raise credits.CreditError("credit_attachment_outcome_unconfirmed")
        return None
    if (
        type(committed) is not dict
        or committed.get("id") != binding["project_id"]
        or type(committed.get("version")) is not int
        or committed["version"] < 1
        or type(committed.get("project")) is not dict
    ):
        raise credits.CreditError("credit_attachment_outcome_unconfirmed")
    project = committed["project"]
    try:
        receipt = project["journey"]["storyChat"]["images"][binding["request_id"]]
    except (KeyError, TypeError):
        raise credits.CreditError("credit_attachment_outcome_unconfirmed") from None
    if binding["request_id"] in images and images[binding["request_id"]] != receipt:
        raise credits.CreditError("credit_attachment_outcome_unconfirmed")
    if (
        type(receipt) is not dict
        or set(receipt) != {"card_id", "asset_id", "entity_id", "at"}
        or receipt["card_id"] != binding["card_id"]
        or not all(
            isinstance(receipt[key], str) and receipt[key]
            for key in ("asset_id", "entity_id")
        )
        or not isinstance(receipt["at"], str)
    ):
        raise credits.CreditError("credit_attachment_outcome_unconfirmed")
    try:
        if datetime.fromisoformat(receipt["at"].replace("Z", "+00:00")).tzinfo is None:
            raise ValueError
    except ValueError:
        raise credits.CreditError("credit_attachment_outcome_unconfirmed") from None
    entities = project.get("entities")
    if not isinstance(entities, list) or not all(
        type(item) is dict for item in entities
    ):
        raise credits.CreditError("credit_attachment_outcome_unconfirmed")
    cards = [item for item in entities if item.get("id") == binding["card_id"]]
    pictures = [item for item in entities if item.get("id") == receipt["entity_id"]]
    if len(cards) != 1 or len(pictures) != 1:
        raise credits.CreditError("credit_attachment_outcome_unconfirmed")
    card, picture = cards[0], pictures[0]
    data = card.get("data")
    picture_data = picture.get("data")
    if type(data) is not dict or type(picture_data) is not dict:
        raise credits.CreditError("credit_attachment_outcome_unconfirmed")
    collections = {"character": "looks", "location": "versions"}
    if card.get("type") in collections:
        variants = data.get(collections[card["type"]], [])
        linked = isinstance(variants, list) and any(
            type(item) is dict
            and isinstance(item.get("imageIds"), list)
            and receipt["entity_id"] in item["imageIds"]
            for item in variants
        )
    else:
        linked = (
            card.get("type") == "prop"
            and isinstance(data.get("imageIds"), list)
            and receipt["entity_id"] in data["imageIds"]
        )
    asset = (
        connection.execute(
            select(asset_table).where(
                asset_table.c.id == receipt["asset_id"],
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
    if (
        not linked
        or picture.get("type") != "image"
        or picture_data.get("cloudAssetId") != receipt["asset_id"]
        or type(record) is not dict
        or record.get("id") != receipt["asset_id"]
        or record.get("project_id") != binding["project_id"]
        or record.get("client_asset_id") != "story-chat-image:" + binding["request_id"]
        or record.get("kind") != "image"
        or record.get("status") != "ready"
        or content_is_deleted(
            connection,
            binding["tenant"],
            binding["owner"],
            asset_id=receipt["asset_id"],
        )
    ):
        raise credits.CreditError("credit_attachment_outcome_unconfirmed")
    return dict(receipt)


def _result(row, entry, created, hold, terminal=None, attachment=None):
    return {
        "account": credits._public_account(row),
        "entry": dict(entry),
        "created": created,
        "hold": dict(hold),
        "terminal": terminal,
        "attachment": attachment,
    }


def service_hold(connection, repo, scope, reference_id, *, binding, quote=None):
    binding, user, hold, terminal = _context(connection, scope, reference_id, binding)
    if hold:
        if quote is not None and _quote(quote) != {
            "credits": hold["held_delta"],
            "price_revision": hold["price_revision"],
        }:
            raise credits.CreditError("credit_idempotency_conflict")
        row = credits._account(
            connection, scope.tenant_id, user["id"], now=repo.clock()
        )
        attachment = None if terminal else _attachment(connection, binding)
        return _result(row, hold, False, hold, terminal, attachment)
    if not credits.credit_policy(connection, scope.tenant_id)["enabled"]:
        return None
    frozen = credits_pricing.image_price(connection, scope.tenant_id)
    if frozen is None:
        raise credits.CreditError("credit_image_price_required", 503)
    frozen = _quote(frozen)
    if quote is not None and _quote(quote) != frozen:
        raise credits.CreditError("credit_idempotency_conflict")
    if user is None or user["status"] != "active":
        raise credits.CreditError("credit_account_unavailable", 403)
    _project(connection, binding)
    try:
        require_credit_limits_reviewed(
            read_settings(connection, scope.tenant_id, scope.owner_id)["value"],
            binding["actor_id"],
        )
    except Conflict:
        raise credits.CreditError("credit_limits_review_required") from None
    check_credit_spending(
        connection, repo, scope, None, frozen["credits"], actor_id=binding["actor_id"]
    )
    row = credits._account(
        connection, scope.tenant_id, user["id"], now=repo.clock(), create=True
    )
    if row["balance"] - row["held"] < frozen["credits"]:
        raise credits.CreditError(
            "insufficient_credits",
            402,
            need=frozen["credits"],
            have=row["balance"] - row["held"],
        )
    result = credits._change(
        connection,
        row,
        kind="hold",
        amount=0,
        held_delta=frozen["credits"],
        reference_type=REFERENCE_TYPE,
        reference_id=reference_id,
        actor=binding["actor_id"],
        reason="Story image reserved",
        idempotency_key=reference_id + ":hold",
        now=repo.clock(),
        price_revision=frozen["price_revision"],
        origin=binding["actor_id"],
        fingerprint=_fingerprint(binding, frozen),
    )
    return {**result, "hold": result["entry"], "terminal": None, "attachment": None}


def _terminal(connection, repo, scope, reference_id, *, binding, kind, reason):
    binding, user, hold, terminal = _context(connection, scope, reference_id, binding)
    if hold is None:
        return None
    row = credits._account(connection, scope.tenant_id, user["id"], now=repo.clock())
    if terminal:
        if terminal["kind"] != kind:
            raise credits.CreditError("credit_terminal_conflict")
        return _result(row, terminal, False, hold, terminal)
    attachment = _attachment(connection, binding, release=kind == "release")
    if kind == "capture" and attachment is None:
        raise credits.CreditError("credit_attachment_required")
    result = credits._change(
        connection,
        row,
        kind=kind,
        amount=-hold["held_delta"] if kind == "capture" else 0,
        held_delta=-hold["held_delta"],
        reference_type=REFERENCE_TYPE,
        reference_id=reference_id,
        actor=hold["actor"],
        reason=reason,
        idempotency_key=reference_id + ":" + kind,
        now=repo.clock(),
        price_revision=hold["price_revision"],
        origin=hold["origin"],
        fingerprint=request_hash({"hold": hold["id"], "kind": kind}),
    )
    return {
        **result,
        "hold": hold,
        "terminal": result["entry"],
        "attachment": attachment,
    }


def service_capture(connection, repo, scope, reference_id, *, binding):
    return _terminal(
        connection,
        repo,
        scope,
        reference_id,
        binding=binding,
        kind="capture",
        reason="Story image captured",
    )


def service_release(connection, repo, scope, reference_id, *, binding, reason):
    if not isinstance(reason, str) or reason not in RELEASE_REASONS:
        raise credits.CreditError("credit_release_reason_invalid", 422)
    return _terminal(
        connection,
        repo,
        scope,
        reference_id,
        binding=binding,
        kind="release",
        reason=reason,
    )
