"""Photos the person attaches in story chat (#726).

An attachment is an ordinary image entity of the story (the composer uploads it with
the Library upload API and adds it with ``asset.attach``). A turn names up to four of
them; the assistant then sees their bounded previews as pictures, and may put one on a
card (``set_card_photo``) or offer to draw a card picture from them (``propose_image``
with ``photos``). No copy of the picture is stored anywhere else.
"""

from __future__ import annotations

MAX_TURN_PHOTOS = 4
# Material photos for one drawn picture; the card's own style picture comes first.
MAX_SOURCE_PHOTOS = 3


class PhotoError(ValueError):
    def __init__(self, code, status=422, **detail):
        self.code, self.status, self.detail = code, status, detail
        super().__init__(code)


def photo_entities(project, ids, limit, removed=()):
    """The story's image entities named by ``ids``, in order; refuses anything else."""
    from .story_chat import retained

    if (
        not isinstance(ids, list)
        or not 1 <= len(ids) <= limit
        or len(set(map(str, ids))) != len(ids)
    ):
        raise PhotoError("story_chat_photos_invalid", max=limit)
    lookup = {e["id"]: e for e in project.get("entities", [])}
    found = []
    for ident in ids:
        entity = lookup.get(ident) if isinstance(ident, str) else None
        asset_id = ((entity or {}).get("data") or {}).get("cloudAssetId")
        if (
            entity is None
            or entity.get("type") != "image"
            or entity["id"] in removed
            or retained(entity)
            or not isinstance(asset_id, str)
        ):
            raise PhotoError("story_chat_photo_missing", 409, ref=ident)
        found.append(
            {
                "id": entity["id"],
                "name": " ".join(str(entity.get("title", "")).split())[:80],
                "asset_id": asset_id,
            }
        )
    return found


def load(assets, principal, project_id, photos):
    """Bounded JPEG previews of owned, ready pictures; one unreadable photo refuses all."""
    from .inky_uploads import preview

    loaded = []
    for photo in photos:
        try:
            jpeg = preview(
                assets,
                principal,
                project_id,
                {"image": {"asset_id": photo["asset_id"]}},
            )
        except Exception:
            raise PhotoError("story_chat_photo_unavailable", ref=photo["id"]) from None
        loaded.append({**photo, "jpeg": jpeg})
    return loaded


def note(photos):
    """What the model is told about the pictures that follow the message."""
    lines = [
        f"Image {number} = photo id {photo['id']}"
        + (f" ({photo['name']})" if photo["name"] else "")
        for number, photo in enumerate(photos, 1)
    ]
    return (
        "\n\nPhotos attached to this message, shown to you as images in this order:\n"
        + "\n".join(lines)
    )


def model_images(loaded):
    return [{"mime": "image/jpeg", "data": item["jpeg"]} for item in loaded]


def read(assets, principal, project_id, project, ids, use):
    """Loaded photos for a chat turn ("turn") or a drawn picture ("picture"); [] for none."""
    if ids is None or ids == []:
        return []
    limit = MAX_TURN_PHOTOS if use == "turn" else MAX_SOURCE_PHOTOS
    return load(assets, principal, project_id, photo_entities(project, ids, limit))


def with_photos(messages, photos):
    """The prompt messages with the photos (and their numbered list) on the last one."""
    if not photos:
        return messages
    last = messages[-1]
    return messages[:-1] + [
        {
            **last,
            "text": last["text"] + note(photos),
            "images": model_images(photos),
        }
    ]


def shown(message, lookup):
    """A stored message for the person's own view: its photos with their picture paths.

    The stored ids stay as they are; a photo since taken out of the story is left out,
    so the bubble shows only pictures that still open.
    """
    from .story_chat import retained

    if not message.get("photos"):
        return message
    files = []
    for ident in message["photos"]:
        entity = lookup.get(ident) or {}
        path = (entity.get("data") or {}).get("cloudContentPath")
        if (
            entity.get("type") == "image"
            and not retained(entity)
            and isinstance(path, str)
            and path.startswith("/v1/assets/")
        ):
            files.append(
                {"id": ident, "name": str(entity.get("title", ""))[:80], "url": path}
            )
    return {**message, "photo_files": files}
