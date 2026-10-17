"""Server-owned recipe source identity, independent from UI/manual version numbers."""
from .repository import request_hash


def source_components(project, shot_id):
    lookup = {e["id"]: e for e in project["entities"]}
    storyboard = "storyboard" in lookup.get(shot_id, {}).get("data", {})
    shot = lookup.get(shot_id, {})
    scene_id = shot.get("parentId")
    chosen, pending = set(), [shot_id]
    incoming = {}
    for link in project["links"]:
        incoming.setdefault(link["target"], []).append(link)
    used_links = {}
    while pending:
        identity = pending.pop()
        if identity in chosen or identity not in lookup:
            continue
        chosen.add(identity)
        node = lookup[identity]
        if node.get("parentId"):
            pending.append(node["parentId"])
        for link in incoming.get(identity, []):
            used_links[link["id"]] = link
            pending.append(link["source"])
        inherited_cast = storyboard and identity == scene_id and "cast" in shot.get("data", {})
        for actor in ([] if inherited_cast else node.get("data", {}).get("cast", [])):
            actor_id = actor.get("characterId") if isinstance(actor, dict) else actor
            if isinstance(actor_id, str) and actor_id in lookup:
                pending.append(actor_id)
        location_id = node.get("data", {}).get("locationId")
        if isinstance(location_id, str) and location_id:
            pending.append(location_id)
        for asset_id in node.get("data", {}).get("referenceAssetIds", []):
            if isinstance(asset_id, str):
                pending.append(asset_id)
        for look in node.get("data", {}).get("looks", []):
            for asset_id in look.get("gallery", {}).values():
                if isinstance(asset_id, str) and asset_id in lookup:
                    pending.append(asset_id)
        if storyboard:
            data = node.get("data", {})
            for ident in data.get("imageIds", []):
                pending.append(ident)
            for look in data.get("looks", []):
                pending.extend(look.get("imageIds", []))
            for voice in data.get("voices", []):
                pending.append(voice["assetId"])
            for version in data.get("versions", []):
                pending.extend(version.get("imageIds", []))
            if data.get("ambient"):
                pending.append(data["ambient"]["assetId"])
            draft = data.get("storyboard", {})
            if draft.get("place"):
                pending.append(draft["place"]["locationId"])
            pending.extend(draft.get("propIds", []))
            pending.extend(ref["entityId"] for ref in draft.get("extraRefs", []))
            pending.extend(filter(None, (draft.get("firstFrameId"), draft.get("lastFrameId"))))
            if draft.get("continuePrevious"):
                from .storyboard import previous_shot
                previous = previous_shot(project, node)
                if previous:
                    pending.append(previous["id"])
                    if previous["data"].get("selectedAssetId"):
                        pending.append(previous["data"]["selectedAssetId"])
                if draft.get("previousFrame"):
                    pending.append(draft["previousFrame"]["frameAssetId"])
        # Guide sources may not also have a visible reference edge.
        for guide in node.get("data", {}).get("h3", {}).get("guides", []):
            if isinstance(guide, dict) and isinstance(guide.get("media_id"), str):
                pending.append(guide["media_id"])
    return {"shot_id": shot_id, "entities": [lookup[k] for k in sorted(chosen)],
                         "links": [used_links[k] for k in sorted(used_links)],
                         **({"storyboard_style": project.get("journey", {}).get("storyboard", {}).get("style", "")}
                            if storyboard else {}),
                         **({"storyboard_defaults": project["journey"]["storyboard"]["defaults"]}
                            if "defaults" in project.get("journey", {}).get("storyboard", {}) else {})}


def source_snapshot(project, shot_id):
    return request_hash(source_components(project, shot_id))


def validate_source_ref(project, ref):
    lookup = {e["id"]: e for e in project["entities"]}
    shot = lookup.get(ref["shot_id"])
    if not shot or shot["type"] not in {"shot", "generation"} or shot["version"] != ref["shot_version"]:
        return False
    ancestors = {}
    node = lookup.get(shot.get("parentId"))
    while node:
        ancestors[node["type"]] = node["id"]
        node = lookup.get(node.get("parentId"))
    return all(ref.get(key) in (None, ancestors.get(kind)) for key, kind in (("chapter_id", "chapter"), ("scene_id", "scene")))
