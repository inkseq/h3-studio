"""Minimal creator context and provenance for advisory-only Inky evaluations."""

from .inky_models import context, language
from .inky_uploads import upload_context
from .repository import request_hash

REVISION = "inky-v3-20261027"
FIELDS = {"needs_keyframe": "frames", "upload_card": "cast"}


def shot_source(project, shot):
    """Never send accepted requests, account settings, URLs, media or model controls.

    An explicit empty shot cast is authoritative. Storyboard cast inheritance
    matches compile_draft; only names of that resolved cast enter the context.
    Oversized or malformed creator context is unavailable, never silently cut
    into a different claim about the story.
    """
    lookup = {e["id"]: e for e in project.get("entities", [])}
    scene = lookup.get(shot.get("parentId"), {})
    data, scene_data = shot.get("data", {}), scene.get("data", {})
    board = data.get("storyboard", {})
    bindings = data.get("cast", scene_data.get("cast", []))
    characters = []
    for binding in bindings:
        character = lookup.get(
            binding if isinstance(binding, str) else binding.get("characterId"), {}
        )
        if character.get("type") != "character":
            raise ValueError("inky_context_invalid")
        if character.get("title") not in characters:
            characters.append(character.get("title"))
    if "place" in board:
        place_id = (board["place"] or {}).get("locationId")
    else:
        place_id = data.get("locationId") or scene_data.get("locationId")
    place = lookup.get(place_id, {})
    props = []
    for ident in board.get("propIds", []):
        prop = lookup.get(ident, {})
        if prop.get("type") != "prop":
            raise ValueError("inky_context_invalid")
        if prop.get("title") not in props:
            props.append(prop.get("title"))
    for link in project.get("links", []):
        entity = lookup.get(link.get("source"), {})
        if link.get("target") == shot["id"] and entity.get("type") == "prop":
            if entity.get("title") not in props:
                props.append(entity.get("title"))
    action = data.get("prompt", shot.get("description", ""))
    value = context(
        {
            "shot_action": action,
            "scene_script": scene_data.get("script", ""),
            "characters": characters,
            "place": place.get("title", ""),
            "props": props,
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
    return {
        "shot_id": shot["id"],
        "base_version": shot.get("version", 1),
        "source_id": source_id,
        "context": value,
        "upload": upload,
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
