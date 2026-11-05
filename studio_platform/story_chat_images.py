"""Card pictures for story chat (#422): Gemini draws a character, place or prop card.

The picture is stored through the existing AssetService.upload and attached with the
existing ``asset.attach`` action, then added to the card the way the Works editor does
(character look ``main``, location version ``main``, prop ``imageIds``). Credits move
only through the billing hook: hold before the Google call, capture after the picture
is on the card, release on any failure. No new table, queue or provider account.
"""

from __future__ import annotations

import base64
import copy
import re

import httpx
from sqlalchemy import select

from .google_chat import ORIGIN, ChatError
from .prompt_files import sections as prompt_sections

MODEL = "gemini-nano-banana-2.1"
REQUEST_ID = re.compile(r"^[A-Za-z0-9_-]{8,80}$")
KINDS = {"character", "location", "prop"}
MAX_IMAGE_BYTES = 8 * 1024 * 1024
ASPECT = {"character": "3:4", "location": "16:9", "prop": "1:1"}
STYLE = prompt_sections("card_picture_styles")
FIRST_FRAME = prompt_sections("first_frame")
MAX_CARD_TEXT = 400


class ImageError(Exception):
    def __init__(self, code, status=502, **detail):
        self.code, self.status, self.detail = code, status, detail
        super().__init__(code)


def aspect_for(card):
    """Pictures of a shot are its first frame, so they take the film's shape."""
    return ASPECT.get(card["type"], "16:9")


def shot_context(project, card):
    """What a shot's picture is drawn from: the same bounded context Inky reads."""
    from .inky_source import shot_source

    try:
        return shot_source(project, card)["context"]
    except (ValueError, TypeError, AttributeError, KeyError):
        return {"shot_action": card.get("description") or card.get("title") or ""}


def reference_cards(project, card):
    """The cards a shot's picture must match, in the order the prompt lists them."""
    from .inky_source import shot_cards
    from .story_chat import card_image

    try:
        cast, place, props = shot_cards(project, card)
    except (ValueError, TypeError, AttributeError, KeyError):
        return []
    lookup = {e["id"]: e for e in project.get("entities", [])}
    rows, seen = [], set()
    for role, cards, limit in (
        ("character", cast, 4),
        ("place", [place] if place else [], 1),
        ("prop", props, 2),
    ):
        for entity in cards:
            if entity["id"] in seen or not limit:
                continue
            seen.add(entity["id"])
            limit -= 1
            image = lookup.get(card_image(entity)) or {}
            rows.append(
                {
                    "role": role,
                    "name": " ".join(str(entity.get("title", "")).split())[:80],
                    "description": " ".join(
                        str(entity.get("description") or "").split()
                    )[:MAX_CARD_TEXT],
                    "asset_id": (image.get("data") or {}).get("cloudAssetId"),
                }
            )
    return rows


def load_pictures(assets, principal, project_id, rows):
    """The pictures of those cards that can be read. A missing one leaves its text in the prompt."""
    from .inky_uploads import preview

    pictures = []
    for row in rows:
        if not row["asset_id"]:
            continue
        try:
            jpeg = preview(
                assets, principal, project_id, {"image": {"asset_id": row["asset_id"]}}
            )
        except Exception:
            continue
        pictures.append({"row": row, "jpeg": jpeg})
    return pictures


def _card_lines(label, rows):
    if not rows:
        return []
    return [
        label
        + ": "
        + "; ".join(
            f"{row['name']} ({row['description']})"
            if row["description"]
            else row["name"]
            for row in rows
        )
    ]


def shot_prompt(project, card, style, instruction, pictures=()):
    """The first moment of a shot (#1039), composed from the shot's own cards."""
    context = shot_context(project, card)
    rows = reference_cards(project, card)
    parts = [
        FIRST_FRAME["open"],
        f"What happens: {context['shot_action'].strip()[:2000]}",
    ]
    for role, label in (
        ("character", "Characters"),
        ("place", "Place"),
        ("prop", "Props"),
    ):
        parts += _card_lines(label, [row for row in rows if row["role"] == role])
    if any(row["role"] == "character" for row in rows):
        parts.append(FIRST_FRAME["only_listed"])
    if pictures:
        parts.append(FIRST_FRAME["attached"])
        parts += [
            FIRST_FRAME[item["row"]["role"]].format(
                number=number, name=item["row"]["name"]
            )
            for number, item in enumerate(pictures, 1)
        ]
    if style.strip():
        parts.append("Overall film style: " + style.strip()[:2000])
    if instruction.strip():
        parts.append("Change requested: " + instruction.strip()[:1000])
    parts.append(FIRST_FRAME["end"])
    return "\n".join(parts)


def prompt_for(project, card, instruction="", pictures=()):
    style = (project.get("journey", {}).get("storyboard") or {}).get("style", "")
    if card["type"] == "shot":
        return shot_prompt(project, card, style, instruction, pictures)
    parts = [
        STYLE[card["type"]],
        f"Subject: {card.get('title', '').strip()}.",
        (card.get("description") or "").strip(),
    ]
    if style.strip():
        parts.append("Overall film style: " + style.strip()[:2000])
    if instruction.strip():
        parts.append("Change requested: " + instruction.strip()[:1000])
    parts.append("No text, captions, watermarks or borders.")
    return "\n".join(p for p in parts if p)


class GeminiImageClient:
    """One bounded image call; no retries, the caller owns any billing decision."""

    def __init__(self, loader, transport=None, *, timeout=None):
        self.loader, self.transport = loader, transport
        self.timeout = timeout or httpx.Timeout(120, connect=15)

    def generate(self, prompt, aspect="1:1", pictures=()):
        from .llm_tracing import clip, generation

        with generation(
            "gemini-picture",
            model=MODEL,
            input=clip(prompt, 2000),
            model_parameters={"aspect": aspect, "pictures": len(pictures)},
        ) as traced:
            mime, data = self._generate(prompt, aspect, pictures)
            traced.update(output={"mime": mime, "bytes": len(data)})
        return mime, data

    def _generate(self, prompt, aspect, pictures=()):
        config = self.loader()
        if config.base_url != ORIGIN or not config.api_key:
            raise ImageError("image_profile_mismatch", 503)
        body = {
            "contents": [
                {
                    "role": "user",
                    "parts": [
                        {"text": prompt},
                        *(
                            {
                                "inlineData": {
                                    "mimeType": "image/jpeg",
                                    "data": base64.b64encode(item["jpeg"]).decode(),
                                }
                            }
                            for item in pictures
                        ),
                    ],
                }
            ],
            "generationConfig": {
                "responseModalities": ["IMAGE"],
                "imageConfig": {"aspectRatio": aspect},
            },
        }
        try:
            with httpx.Client(
                base_url=ORIGIN,
                headers={"x-goog-api-key": config.api_key},
                timeout=self.timeout,
                follow_redirects=False,
                transport=self.transport,
            ) as client:
                response = client.post(
                    f"/v1beta/models/{MODEL}:generateContent", json=body
                )
        except httpx.TimeoutException:
            # The picture may still have been made upstream; never retried automatically.
            raise ImageError("image_upstream_timeout", 504) from None
        except Exception:
            raise ImageError("image_connection_failed", 503) from None
        if response.status_code != 200:
            raise ImageError("image_http_" + str(response.status_code), 502)
        try:
            candidate = response.json()["candidates"][0]
            for part in candidate["content"]["parts"]:
                inline = part.get("inlineData")
                if inline and inline.get("mimeType") in {
                    "image/jpeg",
                    "image/png",
                    "image/webp",
                }:
                    data = base64.b64decode(inline["data"], validate=True)
                    if 0 < len(data) <= MAX_IMAGE_BYTES:
                        return inline["mimeType"], data
        except Exception:
            raise ImageError("image_invalid_response", 502) from None
        # Usually a safety block: no picture, nothing to attach.
        raise ImageError("image_not_returned", 422)


def configured_image_client(settings):
    """Same protected Google credential as titles and story chat; no new secret."""
    from .story_chat_routes import configured_client

    text = configured_client(settings)
    return None if text is None else GeminiImageClient(text.loader)


def attach_actions(project, card, asset_id, image_entity_id):
    """Existing actions that put the picture on the card, newest first as the main one."""
    data = copy.deepcopy(card.get("data", {}))
    if card["type"] == "character":
        looks = data.get("looks") or []
        if not looks:
            looks = [
                {
                    "id": "main",
                    "name": "默认造型",
                    "description": "",
                    "gallery": {},
                    "imageIds": [],
                    "version": 1,
                }
            ]
        default = data.get("defaultLookId") or looks[0]["id"]
        look = next((item for item in looks if item["id"] == default), looks[0])
        look["imageIds"] = [image_entity_id] + [
            i for i in look.get("imageIds", []) if i != image_entity_id
        ]
        look["version"] = look.get("version", 1) + 1
        patch = {"looks": looks, "defaultLookId": look["id"]}
    elif card["type"] == "location":
        versions = data.get("versions") or []
        if not versions:
            versions = [
                {
                    "id": "main",
                    "name": "默认",
                    "description": "",
                    "imageIds": [],
                    "version": 1,
                }
            ]
        default = data.get("defaultVersionId") or versions[0]["id"]
        version = next(
            (item for item in versions if item["id"] == default), versions[0]
        )
        version["imageIds"] = [image_entity_id] + [
            i for i in version.get("imageIds", []) if i != image_entity_id
        ]
        version["version"] = version.get("version", 1) + 1
        patch = {"versions": versions, "defaultVersionId": version["id"]}
    elif card["type"] == "shot":
        # The newest picture is the shot's first frame; earlier ones stay in the story's files.
        patch = {
            "storyboard": {
                **shot_storyboard(data),
                "firstFrameId": image_entity_id,
                "continuePrevious": False,
            }
        }
    else:
        patch = {
            "imageIds": [image_entity_id]
            + [i for i in data.get("imageIds", []) if i != image_entity_id]
        }
    return [
        {
            "op": "asset.attach",
            "asset_id": asset_id,
            "entity_id": image_entity_id,
            "title": card.get("title") or "picture",
        },
        {"op": "entity.update", "entity_id": card["id"], "patch": {"data": patch}},
    ]


RELEASE_REASON = {
    "image_upstream_timeout": "provider_timeout_possible_output",
    "story_chat_card_missing": "card_missing",
    "document_version_conflict": "version_conflict",
    "image_store_failed": "store_failed",
}


def shot_storyboard(data):
    """A shot's generation settings. One with none gets the default block; the route has already
    refused any shot whose older settings a new block would lose."""
    return data.get("storyboard") or {"version": 1, "mode": "fl"}


def older_settings_in_use(project, card):
    """Saved through the older settings path, without a storyboard block. Adding a block moves the
    shot to the storyboard compiler, which ignores those inputs, so the shot could no longer make
    its video. Such a shot keeps its settings and gets no picture here."""
    data = card.get("data") or {}
    if data.get("storyboard"):
        return False
    h3 = data.get("h3")
    if isinstance(h3, dict) and any(
        h3.get(key) for key in ("recipeId", "inputMode", "guides")
    ):
        return True
    return any(link.get("target") == card["id"] for link in project.get("links", []))


def input_hash(project, card, instruction):
    """What a picture was drawn from; a replay with different material conflicts."""
    import hashlib
    import json

    style = (project.get("journey", {}).get("storyboard") or {}).get("style", "")
    value = {
        "name": " ".join(str(card.get("title", "")).split()),
        "description": " ".join(str(card.get("description", "")).split()),
        "style": " ".join(str(style).split()),
        "instruction": " ".join(str(instruction).split()),
    }
    if card.get("type") == "shot":
        action = (card.get("data") or {}).get("prompt", "")
        value["action"] = " ".join(str(action).split())
        # Everything else the prompt reads, so an edit to the cast, place or props is noticed.
        value["older_settings"] = older_settings_in_use(project, card)
        context = shot_context(project, card)
        value["context"] = {
            key: context.get(key)
            for key in ("shot_action", "characters", "place", "props")
        }
        value["references"] = reference_cards(project, card)
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


class ServiceCreditBilling:
    """The audited story-image credit primitive (#463/#464): hold, capture, release.

    Each call runs in its own short repository transaction; none is held across the
    Google call. While customer charging is off, hold returns None and nothing moves.
    """

    def __init__(self, app):
        self.app = app

    def _run(self, name, *args, **kwargs):
        from . import service_credits

        repo = self.app.state.repository
        with repo.transaction() as connection:
            return getattr(service_credits, name)(connection, repo, *args, **kwargs)

    def price(self, tenant):
        from .credits_pricing import image_price

        repo = self.app.state.repository
        with repo.engine.connect() as connection:
            try:
                return image_price(connection, tenant)
            except Exception:
                return None

    def has_hold(self, scope, reference):
        """Read-only: does the ledger already hold credits for this request?"""
        from . import credits
        from .credit_schema import entries
        from .service_credits import REFERENCE_TYPE

        repo = self.app.state.repository
        with repo.engine.connect() as connection:
            if not credits._ready(connection):
                return False
            try:
                user = credits._user(connection, scope.tenant_id, scope.owner_id)
            except credits.CreditError:
                return False
            return (
                connection.execute(
                    select(entries.c.id).where(
                        entries.c.tenant == scope.tenant_id,
                        entries.c.user_id == user["id"],
                        entries.c.reference_type == REFERENCE_TYPE,
                        entries.c.reference_id == reference,
                        entries.c.kind == "hold",
                    )
                ).first()
                is not None
            )

    def hold(self, scope, reference, binding):
        return self._run("service_hold", scope, reference, binding=binding)

    def capture(self, scope, reference, binding):
        return self._run("service_capture", scope, reference, binding=binding)

    def release(self, scope, reference, binding, reason):
        return self._run(
            "service_release", scope, reference, binding=binding, reason=reason
        )


__all__ = [
    "MODEL",
    "REQUEST_ID",
    "KINDS",
    "ASPECT",
    "ImageError",
    "ChatError",
    "GeminiImageClient",
    "ServiceCreditBilling",
    "RELEASE_REASON",
    "input_hash",
    "attach_actions",
    "configured_image_client",
    "prompt_for",
]
