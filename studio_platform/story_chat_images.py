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


class NoBilling:
    """Until the credit service charge exists: free only while charging is off."""

    def __init__(self, app):
        self.app = app

    def _enabled(self, principal):
        from .credits import credit_policy

        repo = self.app.state.repository
        with repo.engine.connect() as connection:
            return credit_policy(connection, self.app.state.settings.tenant_id)[
                "enabled"
            ]

    def hold(self, principal, reference_id):
        if self._enabled(principal):
            raise ImageError("image_billing_unavailable", 503)
        return None

    def capture(self, principal, reference_id):
        return None

    def release(self, principal, reference_id, reason):
        return None


__all__ = [
    "MODEL",
    "REQUEST_ID",
    "KINDS",
    "ASPECT",
    "ImageError",
    "ChatError",
    "GeminiImageClient",
    "NoBilling",
    "attach_actions",
    "configured_image_client",
    "prompt_for",
]
