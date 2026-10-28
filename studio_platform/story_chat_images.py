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

MODEL = "gemini-nano-banana-2.1"
REQUEST_ID = re.compile(r"^[A-Za-z0-9_-]{8,80}$")
KINDS = {"character", "location", "prop"}
MAX_IMAGE_BYTES = 8 * 1024 * 1024
ASPECT = {"character": "3:4", "location": "16:9", "prop": "1:1"}
STYLE = {
    "character": "Character reference sheet for an animated short film: full body, front view, "
    "neutral pose, plain light-grey background, soft even studio light.",
    "location": "Establishing reference image of a film location, wide shot, no people, "
    "cinematic natural light.",
    "prop": "Product-style reference image of a single prop, centred, plain light-grey "
    "background, soft studio light.",
}


class ImageError(Exception):
    def __init__(self, code, status=502, **detail):
        self.code, self.status, self.detail = code, status, detail
        super().__init__(code)


def prompt_for(project, card, instruction=""):
    style = (project.get("journey", {}).get("storyboard") or {}).get("style", "")
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

    def generate(self, prompt, aspect="1:1"):
        from .llm_tracing import clip, generation

        with generation(
            "gemini-picture",
            model=MODEL,
            input=clip(prompt, 2000),
            model_parameters={"aspect": aspect},
        ) as traced:
            mime, data = self._generate(prompt, aspect)
            traced.update(output={"mime": mime, "bytes": len(data)})
        return mime, data

    def _generate(self, prompt, aspect):
        config = self.loader()
        if config.base_url != ORIGIN or not config.api_key:
            raise ImageError("image_profile_mismatch", 503)
        body = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
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
