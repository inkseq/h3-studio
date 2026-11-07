"""Minimal creator context and provenance for advisory-only Inky evaluations."""

from .inky_models import context, language
from .inky_uploads import upload_context
from .repository import request_hash

REVISION = "inky-v3-20261027"
FIELDS = {"needs_keyframe": "frames", "upload_card": "cast"}


def shot_cards(project, shot):
    """The character, place and prop cards a shot uses; cast inheritance matches compile_draft."""
    lookup = {e["id"]: e for e in project.get("entities", [])}
    scene = lookup.get(shot.get("parentId"), {})
    data, scene_data = shot.get("data", {}), scene.get("data", {})
    board = data.get("storyboard", {})
    cast = []
    for binding in data.get("cast", scene_data.get("cast", [])):
        character = lookup.get(
            binding if isinstance(binding, str) else binding.get("characterId"), {}
        )
        if character.get("type") != "character":
            raise ValueError("inky_context_invalid")
        cast.append(character)
    if "place" in board:
        place_id = (board["place"] or {}).get("locationId")
    else:
        place_id = data.get("locationId") or scene_data.get("locationId")
    props = []
    for ident in board.get("propIds", []):
        prop = lookup.get(ident, {})
        if prop.get("type") != "prop":
            raise ValueError("inky_context_invalid")
        props.append(prop)
    for link in project.get("links", []):
        entity = lookup.get(link.get("source"), {})
        if link.get("target") == shot["id"] and entity.get("type") == "prop":
            props.append(entity)
    return cast, lookup.get(place_id, {}), props


def _titles(cards):
    return list(dict.fromkeys(card.get("title") for card in cards))


def shot_source(project, shot):
    """Never send accepted requests, account settings, URLs, media or model controls.

    An explicit empty shot cast is authoritative. Storyboard cast inheritance
    matches compile_draft; only names of that resolved cast enter the context.
    Oversized or malformed creator context is unavailable, never silently cut
    into a different claim about the story.
    """
    data = shot.get("data", {})
    cast, place, props = shot_cards(project, shot)
    scene = next(
        (e for e in project.get("entities", []) if e["id"] == shot.get("parentId")), {}
    )
    scene_data = scene.get("data", {})
    action = data.get("prompt", shot.get("description", ""))
    value = context(
        {
            "shot_action": action,
            "scene_script": scene_data.get("script", ""),
            "characters": _titles(cast),
            "place": place.get("title", ""),
            "props": _titles(props),
            "lang": language(action),
            "seconds": data.get("seconds", 5),
        }
    )
    # Include all shot data in the local digest so a new keyframe/assignment
    # invalidates an old recommendation even if an importer omitted a version.
    # Only the minimal context above can cross the provider boundary.
    upload = upload_context(project, shot)
    source_id = request_hash(
        {
            "revision": REVISION,
            "shot": shot,
            "context": value,
            "upload": upload,
            "links": [
                link
                for link in project.get("links", [])
                if link.get("target") == shot["id"]
            ],
        }
    )
    board = data.get("storyboard") or {}
    return {
        "shot_id": shot["id"],
        "base_version": shot.get("version", 1),
        "source_id": source_id,
        "context": value,
        "upload": upload,
        # Local only, never sent to a provider: a shot that already has its first
        # frame does not need the keyframe tip (owner review, 2026-11-07).
        "has_first_frame": bool(
            board.get("firstFrameId") or board.get("continuePrevious")
        ),
    }


def sources(project):
    result = {}
    for shot in project.get("entities", []):
        if shot.get("type") != "shot":
            continue
        try:
            result[shot["id"]] = shot_source(project, shot)
        except (ValueError, TypeError, AttributeError, KeyError):
            # Advice is optional; malformed context is not permission to guess.
            continue
    return result


def suggestion_id(source, kind):
    return request_hash({"source": source["source_id"], "kind": kind})


def item(source, kind, example=None):
    value = {
        "id": suggestion_id(source, kind),
        "kind": kind,
        "shot_id": source["shot_id"],
        "field": FIELDS.get(kind, "action"),
        "base_version": source["base_version"],
        "tip_key": "inky." + kind,
        "lang": source["context"]["lang"],
    }
    if example:
        value["example"] = example
    return value
