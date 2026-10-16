"""Additive storyboard draft fields and pure compilation of existing entities.

This module owns no projects, jobs, assets, permissions, budgets or providers.
Only explicitly saved storyboard version 1 shots use these rules. All IDs here
are document entity IDs; executable inputs use their existing asset receipts.
"""
from __future__ import annotations

import copy
import math


SHOT_FIELDS = {"version", "mode", "quality", "place", "propIds", "firstFrameId",
               "lastFrameId", "continuePrevious", "extraRefs", "previousFrame"}


def contract():
    """Discoverable additive fields; existing document/action APIs remain used."""
    from .storyboard_versions import contract as versions_contract
    ident = {"type": "string", "pattern": "^[A-Za-z0-9_-]{1,160}$"}
    ids = {"type": "array", "maxItems": 100, "uniqueItems": True, "items": ident}
    start = {"type": "number", "minimum": 0, "maximum": 360000, "default": 0}
    return {"version": 1, "version_workflow": versions_contract(), "entity_id_semantics": "All profile/shot image, voice and frame IDs are existing same-project document entity IDs, not upload receipt IDs.",
        "fields": {"character.data": {"defaultLookId": ident, "mainVoiceId": ident,
            "voices": {"type": "array", "maxItems": 100, "items": {"type": "object", "additionalProperties": False,
                "required": ["id", "assetId"], "properties": {"id": ident, "assetId": ident, "clipStart": start}}}},
            "character.data.looks[]": {"imageIds": ids, "voiceId": ident,
                "notes": "Keep existing id/name/description/version/gallery; first image is main, absent imageIds uses legacy gallery. Empty voiceId uses main voice."},
            "location.data": {"defaultVersionId": ident, "versions": {"type": "array", "maxItems": 100,
                "items": {"type": "object", "additionalProperties": False, "required": ["id", "name", "version"],
                    "properties": {"id": ident, "name": {"type": "string", "minLength": 1, "maxLength": 160},
                        "description": {"type": "string", "maxLength": 24000}, "imageIds": ids,
                        "version": {"type": "integer", "minimum": 1}}}},
                "ambient": {"anyOf": [{"type": "null"}, {"type": "object", "additionalProperties": False,
                    "required": ["assetId"], "properties": {"assetId": ident, "clipStart": start}}]}},
            "prop.data": {"imageIds": ids}, "project.journey.storyboard": {"style": {"type": "string", "maxLength": 12000}},
            "shot.data.storyboard": {"type": "object", "additionalProperties": False, "required": ["version", "mode"],
                "properties": {"version": {"const": 1}, "mode": {"enum": ["fl", "ref"]}, "quality": {"enum": ["draft", "final"], "default": "draft"},
                    "place": {"anyOf": [{"type": "null"}, {"type": "object", "additionalProperties": False,
                        "required": ["locationId"], "properties": {"locationId": ident, "versionId": {"type": "string", "maxLength": 160}}}]},
                    "propIds": ids, "firstFrameId": {"type": "string", "maxLength": 160}, "lastFrameId": {"type": "string", "maxLength": 160},
                    "continuePrevious": {"type": "boolean", "default": False}, "extraRefs": {"type": "array", "maxItems": 100,
                        "items": {"type": "object", "additionalProperties": False, "required": ["entityId"],
                            "properties": {"entityId": ident, "clipStart": start}}},
                    "previousFrame": {"anyOf": [{"type": "null"}, {"type": "object", "additionalProperties": False,
                        "required": ["shotId", "selectedAssetId", "frameAssetId"], "properties": {"shotId": ident, "selectedAssetId": ident, "frameAssetId": ident}}]}}}},
        "generation_draft_extension": {"storyboard": {"prompt_parts": "Ordered kind/label/text/entity_id? entries, joined with newlines to draft.prompt.",
            "references": "entity_id,asset_id,slot,why,sent,reason,source_range?; omitted items never enter active draft.inputs.",
            "notes": "Nonblocking preservation/missing-photo explanations.", "limits": "Actual selected capability limits, null means not yet read.",
            "previous_shot_id": "Immediate prior shot in existing ordered chapter or null."}},
        "rules": ["FL/REF switching preserves inactive storyboard inputs; only explicit version 1 shots opt into this compiler.",
            "For storyboard shots, present shot.data.cast is the exact override (including an empty list); only absent cast inherits the scene. Explicit storyboard.place=null clears inherited place.",
            "Empty cast lookId uses default look. Empty location versionId uses default version. Existing explicit IDs never silently select a different variant.",
            "Voice/ambient/extra video or audio uses a three-second selected source clip; server asset derivation and real inspection remain authoritative.",
            "Exact h3.deploymentProfileId/recipeId/controls are retained. quality is authoring intent, not permission or automatic profile/model replacement.",
            "continuePrevious requires an owned saved last-frame asset from the immediate previous selected video artifact; admission verifies the original receipt.",
            "Photo/voice/location/prop/style/previous-selection changes invalidate old preflight source identity."]}


def _fields(value, allowed, label):
    if not isinstance(value, dict) or set(value) - set(allowed):
        raise ValueError(label + "包含未知字段或不是对象")


def _id(value, *, empty=False):
    from .project_validation import ID
    if not isinstance(value, str) or not (empty and value == "" or ID.fullmatch(value)):
        raise ValueError("分镜档案身份无效")


def _ids(value):
    if not isinstance(value, list) or len(value) > 100:
        raise ValueError("分镜档案图片或绑定列表无效")
    for ident in value:
        _id(ident)
    if len(set(value)) != len(value):
        raise ValueError("分镜档案图片或绑定列表重复")


def _reference(lookup, ident, kind):
    _id(ident)
    if lookup.get(ident, {}).get("type") != kind:
        raise ValueError("分镜档案引用的" + kind + "不存在或类型错误")


def _clip(value, lookup, *, sample=True):
    _fields(value, {"assetId", "clipStart", *(["id"] if sample else [])}, "声音选段")
    _reference(lookup, value.get("assetId"), "audio")
    start = value.get("clipStart", 0)
    if type(start) not in (int, float) or not math.isfinite(start) or not 0 <= start <= 360000:
        raise ValueError("声音clipStart须为非负有限秒数")
    metadata = lookup[value["assetId"]]["data"].get("metadata", {})
    duration = metadata.get("source_duration") if isinstance(metadata, dict) else None
    if type(duration) in (int, float) and start + 3 > duration + .01:
        raise ValueError("声音三秒选段超出原文件")


def validate(project, lookup):
    """Validate new fields without modifying or upgrading old documents."""
    style = project.get("journey", {}).get("storyboard")
    if style is not None:
        _fields(style, {"style", "defaults"}, "全片风格")
        if "defaults" in style:
            from .storyboard_versions import validate_defaults
            validate_defaults(style["defaults"])
        if not isinstance(style.get("style", ""), str) or len(style.get("style", "")) > 12000:
            raise ValueError("全片风格须为不超过12000字符的文本")
    for entity in project["entities"]:
        kind, data = entity["type"], entity["data"]
        if "imageIds" in data:
            if kind != "prop":
                raise ValueError("顶层imageIds仅用于道具")
            _ids(data["imageIds"])
            for ident in data["imageIds"]:
                _reference(lookup, ident, "image")
        if kind == "character":
            looks, voices = data.get("looks", []), data.get("voices", [])
            if not isinstance(voices, list) or len(voices) > 100:
                raise ValueError("人物声音样本列表无效")
            voice_ids = set()
            for voice in voices:
                _clip(voice, lookup)
                _id(voice.get("id"))
                if voice["id"] in voice_ids:
                    raise ValueError("人物声音样本ID重复")
                voice_ids.add(voice["id"])
            if data.get("mainVoiceId") not in (None, ""):
                _id(data["mainVoiceId"])
                if data["mainVoiceId"] not in voice_ids:
                    raise ValueError("人物主声音不存在")
            if data.get("defaultLookId") not in (None, ""):
                _id(data["defaultLookId"])
                if not any(v["id"] == data["defaultLookId"] for v in looks):
                    raise ValueError("人物默认造型不存在")
            for look in looks:
                if "imageIds" in look:
                    _ids(look["imageIds"])
                    for ident in look["imageIds"]:
                        _reference(lookup, ident, "image")
                if look.get("voiceId") not in (None, ""):
                    _id(look["voiceId"])
                    if look["voiceId"] not in voice_ids:
                        raise ValueError("造型声音不存在")
        elif any(key in data for key in ("voices", "mainVoiceId", "defaultLookId")):
            raise ValueError("人物声音或默认造型仅用于人物")
        if "versions" in data:
            if kind != "location" or not isinstance(data["versions"], list) or len(data["versions"]) > 100:
                raise ValueError("场景版本列表无效")
            seen = set()
            for version in data["versions"]:
                _fields(version, {"id", "name", "description", "imageIds", "version"}, "场景版本")
                _id(version.get("id"))
                if (version["id"] in seen or not isinstance(version.get("name"), str)
                        or not version["name"].strip() or len(version["name"]) > 160
                        or not isinstance(version.get("description", ""), str) or len(version.get("description", "")) > 24000
                        or type(version.get("version")) is not int or version["version"] < 1):
                    raise ValueError("场景版本内容无效")
                seen.add(version["id"])
                _ids(version.get("imageIds", []))
                for ident in version.get("imageIds", []):
                    _reference(lookup, ident, "image")
            if data.get("defaultVersionId") not in (None, ""):
                _id(data["defaultVersionId"])
                if data["defaultVersionId"] not in seen:
                    raise ValueError("默认场景版本不存在")
        if "defaultVersionId" in data and kind != "location":
            raise ValueError("默认场景版本仅用于场景")
        if kind == "location" and data.get("defaultVersionId") not in (None, "") and "versions" not in data:
            raise ValueError("默认场景版本不存在")
        if data.get("ambient") is not None:
            if kind != "location":
                raise ValueError("环境声仅用于场景")
            _clip(data["ambient"], lookup, sample=False)
        if "storyboard" not in data:
            continue
        if kind != "shot":
            raise ValueError("分镜生成配置仅用于镜头")
        value = data["storyboard"]
        _fields(value, SHOT_FIELDS, "分镜生成配置")
        if (type(value.get("version")) is not int or value["version"] != 1
                or not isinstance(value.get("mode"), str) or value["mode"] not in {"fl", "ref"}
                or not isinstance(value.get("quality", "draft"), str) or value.get("quality", "draft") not in {"draft", "final"}
                or type(value.get("continuePrevious", False)) is not bool):
            raise ValueError("分镜生成版本、模式或档次无效")
        for key in ("firstFrameId", "lastFrameId"):
            if key in value:
                _id(value[key], empty=True)
            if value.get(key):
                _reference(lookup, value[key], "image")
        _ids(value.get("propIds", []))
        for ident in value.get("propIds", []):
            _reference(lookup, ident, "prop")
        if value.get("place") is not None:
            place = value["place"]
            _fields(place, {"locationId", "versionId"}, "镜头场景")
            _reference(lookup, place.get("locationId"), "location")
            _id(place.get("versionId", ""), empty=True)
            if place.get("versionId") and not any(v["id"] == place["versionId"] for v in lookup[place["locationId"]]["data"].get("versions", [])):
                raise ValueError("镜头场景版本不存在")
        extra = value.get("extraRefs", [])
        if not isinstance(extra, list) or len(extra) > 100:
            raise ValueError("附加参考列表无效")
        seen = set()
        for ref in extra:
            _fields(ref, {"entityId", "clipStart"}, "附加参考")
            ident = ref.get("entityId")
            _id(ident)
            if ident in seen or lookup.get(ident, {}).get("type") not in {"image", "video", "audio"}:
                raise ValueError("附加参考不存在、重复或类型错误")
            seen.add(ident)
            if "clipStart" in ref:
                start = ref["clipStart"]
                if (lookup[ident]["type"] == "image" or type(start) not in (int, float)
                        or not math.isfinite(start) or not 0 <= start <= 360000):
                    raise ValueError("附加参考clipStart无效")
            metadata = lookup[ident]["data"].get("metadata", {})
            duration = metadata.get("source_duration") if isinstance(metadata, dict) else None
            if (lookup[ident]["type"] in {"video", "audio"} and type(duration) in (int, float)
                    and ref.get("clipStart", 0) + 3 > duration + .01):
                raise ValueError("附加参考三秒选段超出原文件")
        previous = value.get("previousFrame")
        if previous is not None:
            _fields(previous, {"shotId", "selectedAssetId", "frameAssetId"}, "上一镜末帧来源")
            for key in ("shotId", "selectedAssetId", "frameAssetId"):
                _id(previous.get(key))


def previous_shot(project, shot):
    from .render_plans import ordered_chapter
    lookup = {entity["id"]: entity for entity in project["entities"]}
    scene = lookup.get(shot.get("parentId"), {})
    if not scene.get("parentId"):
        return None
    shots = ordered_chapter(project, scene["parentId"])[2]
    index = next((i for i, value in enumerate(shots) if value["id"] == shot["id"]), -1)
    return shots[index - 1] if index > 0 else None


def look_images(look):
    # Explicit [] clears photos; missing field uses the existing fixed gallery.
    return look.get("imageIds", list(dict.fromkeys(filter(None, look.get("gallery", {}).values()))))


def _variant(profile, selected, field, default):
    values = profile["data"].get(field, [])
    ident = selected or profile["data"].get(default)
    return next((value for value in values if value["id"] == ident), None) or (values[0] if not ident and values else None)


def _limits(capabilities, recipe, profile_id, mode):
    if capabilities is None:
        return None
    support = None
    if profile_id is not None:
        profile = next((item for item in capabilities.get("deployment_profiles", []) if item["id"] == profile_id), None)
        support = profile.get("generation_support", {}).get(mode) if profile else None
        if support is None:
            return None
    else:
        support = recipe
    result = copy.deepcopy(support.get("limits", {}))
    constraints = support.get("execution_policy_constraints") or support.get("constraints") or support.get("execution_support", {}).get("constraints", {})
    inputs = constraints.get("input_limits", {})
    for key in ("max_images", "max_videos", "max_audios"):
        if key in inputs:
            result[key] = min(result.get(key, inputs[key]), inputs[key])
    if "max_reference_files" in constraints:
        result["max_total_files"] = min(result.get("max_total_files", constraints["max_reference_files"]), constraints["max_reference_files"])
    for kind in ("video", "audio"):
        key = "max_" + kind + "_duration_seconds"
        if key in inputs:
            target = "max_total_" + kind + "_duration"
            result[target] = min(result.get(target, inputs[key]), inputs[key])
    return result


def compile_draft(project, shot, *, capabilities=None, recipes=None):
    """Return executable active inputs plus a transparent, non-mutating preview."""
    from .capabilities import RECIPES
    value, h3 = shot["data"]["storyboard"], shot["data"].get("h3", {})
    mode = value["mode"]
    available = capabilities.get("recipes", []) if capabilities else recipes or [{"id": key, "mode": m} for key, m in RECIPES.items()]
    chosen = h3.get("recipeId")
    recipe = next((item for item in available if item["id"] == chosen), None) if chosen else next((item for item in available if item["mode"] == mode), None)
    issues, notes, parts, references = [], [], [], []
    if recipe is None or recipe.get("mode") != mode:
        issues.append("分镜模式与保存配方不一致，请明确选择后重新预检")
    lookup = {entity["id"]: entity for entity in project["entities"]}
    profile_id = h3.get("deploymentProfileId")
    from .h3_profile_support import full_controls
    try:
        native_roles = bool(profile_id and full_controls(profile_id))
    except ValueError:
        native_roles = False  # The unresolved selected profile is reported below.
    limits = _limits(capabilities, recipe or {}, profile_id, mode)
    if mode == "ref" and limits is None:
        issues.append("当前参考输入范围尚未读取，请刷新生成能力")
    style = project.get("journey", {}).get("storyboard", {}).get("style", "")
    if style.strip():
        parts.append({"kind": "style", "label": "全片风格", "text": style.strip()})
    scene = lookup.get(shot.get("parentId"), {})
    place = value.get("place")
    if place is None and "place" not in value and (shot["data"].get("locationId") or scene.get("data", {}).get("locationId")):
        place = {"locationId": shot["data"].get("locationId") or scene["data"]["locationId"], "versionId": ""}
    location = lookup.get(place["locationId"]) if place else None
    location_version = _variant(location, place.get("versionId"), "versions", "defaultVersionId") if location else None
    if location:
        description = location["description"]
        if location_version:
            description += "；" + location_version["name"] + "：" + location_version.get("description", "")
        parts.append({"kind": "place", "label": "场景", "entity_id": location["id"], "text": location["title"] + "：" + description})
    cast = []
    bindings = shot["data"].get("cast", scene.get("data", {}).get("cast", []))
    for binding in bindings:
        ident = binding if isinstance(binding, str) else binding["characterId"]
        if any(item[0]["id"] == ident for item in cast):
            continue
        character = lookup[ident]
        look = _variant(character, binding.get("lookId") if isinstance(binding, dict) else "", "looks", "defaultLookId")
        text = character["title"] + "：" + character["description"]
        if look:
            text += "；" + look["name"] + "：" + look.get("description", "")
        parts.append({"kind": "cast", "label": "人物", "entity_id": ident, "text": text})
        cast.append((character, look))
    for ident in value.get("propIds", []):
        prop = lookup[ident]
        parts.append({"kind": "prop", "label": "道具", "entity_id": ident, "text": prop["title"] + "：" + prop["description"]})
    action = shot["data"].get("prompt", shot.get("description", ""))
    if action.strip():
        parts.append({"kind": "action", "label": "动作", "text": action.strip()})
    else:
        issues.append("还没写这一镜的动作描述")
    prompt = "\n".join(part["text"] for part in parts)
    if len(prompt) > 12000:
        issues.append("组装后的提示词超过12000字符，请缩短档案或动作描述")
    inputs = {"images": [], "videos": [], "audios": [], "first_frame": None, "last_frame": None, "guides": []}

    def add(ident, why, *, start=None, slot=None, guide=None, clip_seconds=3):
        entity = lookup.get(ident)
        if not entity:
            issues.append("参考素材不存在，请重新选择")
            return
        target = slot or {"image": "images", "video": "videos", "audio": "audios"}.get(entity["type"])
        receipt = entity['data'].get('cloudAssetId')
        entry = {"media_id" if target=='guides' else "asset_id": receipt}
        ref = {"entity_id": ident, "asset_id": receipt, "slot": target, "why": why, "sent": False, "reason": ""}
        if guide is not None:
            entry.update(time_seconds=guide.get('time_seconds'),use_audio=guide.get('use_audio',False))
            selected = guide.get('source_range')
            if selected is not None:
                from .generation_draft import validate_range
                if not isinstance(selected, dict):
                    ref['reason'] = '参考选段格式无效'
                else:
                    if selected.get('fileId') != entity['data'].get('fileId'):
                        ref['reason'] = '原素材文件已改变，请重新确认选段'
                    selected = {key: selected.get(key) for key in ('start', 'end')}
                    try:
                        validate_range(selected)
                    except ValueError as error:
                        ref['reason'] = str(error)
                    entry['source_range'] = selected
                    ref['source_range'] = copy.deepcopy(selected)
        if start is not None:
            entry["source_range"] = {"start": start, "end": start + clip_seconds}
            ref["source_range"] = copy.deepcopy(entry["source_range"])
        if target == "videos":
            entry["include_audio"] = h3.get('video_audio',{}).get(ident,False) if native_roles else False
        if not receipt or not entity["data"].get("fileId") or entity["data"].get("missingFile"):
            ref["reason"] = "素材尚未同步为可用的云端输入"
        references.append((ref, entry))

    def extra_reference(item):
        entity=lookup[item['entityId']]
        seconds=3
        if (native_roles and entity['type']=='video'
                and h3.get('controls',{}).get('video_conditioning') in {'denoise','in_context'}):
            selected=h3.get('controls',{}).get('duration')
            seconds=selected if selected is not None else shot['data'].get('seconds',5)
            if type(seconds) is not int or not 4<=seconds<=15:
                issues.append('控制视频需要明确的4–15秒生成时长')
                return
            metadata=entity['data'].get('metadata',{})
            available=metadata.get('source_duration') if isinstance(metadata,dict) else None
            if available is None and isinstance(metadata,dict):
                available=metadata.get('duration')
            if available is None:
                available=entity['data'].get('duration')
            if type(available) not in (int,float) or not math.isfinite(available):
                issues.append('控制视频缺少已核验的原素材时长，请刷新素材')
                return
            if item.get('clipStart',0)+seconds>available+.01:
                issues.append('控制视频选段超出原素材，请调整起点、生成时长或视频')
                return
        # The source selection remains within 2–15 seconds. Existing media
        # normalization then pads its tail onto the same native output grid;
        # 5 selected seconds become 124 frames, 15 become 362 frames.
        add(entity['id'],'附加素材',start=item.get('clipStart',0) if entity['type'] in {'video','audio'} else None,
            clip_seconds=seconds)

    previous = previous_shot(project, shot)
    if mode == "fl" or native_roles:
        first = value.get("firstFrameId")
        if value.get("continuePrevious"):
            bound = value.get("previousFrame")
            selected = previous["data"].get("selectedAssetId") if previous else None
            candidate = lookup.get(selected, {})
            first = bound.get("frameAssetId") if bound else None
            image = lookup.get(first, {})
            provenance = image.get("data", {}).get("sourceSelection", {})
            if (not previous or not selected or not bound or bound.get("shotId") != previous["id"]
                    or bound.get("selectedAssetId") != selected or candidate.get("type") != "video"
                    or not candidate.get("data", {}).get("cloudArtifactId")
                    or provenance.get("kind") != "artifact_frame" or provenance.get("position") != "last"
                    or provenance.get("artifact_id") != candidate["data"]["cloudArtifactId"]):
                issues.append("上一镜选定版本或末帧来源已变化，请重新保存上一镜末帧")
                first = None
        if first:
            add(first, "上一镜最后一帧" if value.get("continuePrevious") else "开头画面", slot="first_frame")
        if value.get("lastFrameId"):
            add(value["lastFrameId"], "结尾画面", slot="last_frame")
    if mode == "fl":
        if cast or location or value.get("propIds"):
            notes.append("首尾帧模式的人物、场景和道具只用文字描述")
        for item in value.get('extraRefs',[]) if native_roles else []:
            if lookup[item['entityId']]['type'] in {'video','audio'}:
                extra_reference(item)
        if any(lookup[item['entityId']]['type']=='image' for item in value.get('extraRefs',[])) or (
                value.get('extraRefs') and not native_roles):
            notes.append("全能参考的附加素材已保留，当前不发送")
    else:
        for character, look in cast:
            images = look_images(look) if look else []
            if images:
                add(images[0], character["title"] + " · " + look["name"])
            else:
                notes.append(character["title"] + "没有造型主图，只使用文字描述")
            voice_id = (look or {}).get("voiceId") or character["data"].get("mainVoiceId")
            voice = next((v for v in character["data"].get("voices", []) if v["id"] == voice_id), None)
            if voice:
                add(voice["assetId"], character["title"] + "的声音", start=voice.get("clipStart", 0))
        if location:
            images = location_version.get("imageIds", []) if location_version else location["data"].get("referenceAssetIds", [])
            if images:
                add(images[0], "场景")
        for ident in value.get("propIds", []):
            prop = lookup[ident]
            if prop["data"].get("imageIds"):
                add(prop["data"]["imageIds"][0], "道具 · " + prop["title"])
        for ref in value.get("extraRefs", []):
            extra_reference(ref)
        if location and location["data"].get("ambient"):
            ambient = location["data"]["ambient"]
            add(ambient["assetId"], "场景环境声", start=ambient.get("clipStart", 0))
        if not native_roles and (value.get("firstFrameId") or value.get("lastFrameId") or value.get("continuePrevious")):
            notes.append("首尾帧输入已保留，全能参考模式不发送")
    if native_roles:
        # These are the existing shot.configure_generation guide associations,
        # not inferred guide roles for character photos or storyboard extras.
        for guide in h3.get('guides',[]):
            if not isinstance(guide,dict):
                issues.append('时间锚点格式无效')
                continue
            add(guide.get('media_id'),'时间锚点',slot='guides',guide=guide)
    counters, total, seen = {}, 0, set()
    durations = {"videos": 0., "audios": 0.}
    for ref, entry in references:
        slot, identity = ref["slot"], ref["asset_id"]
        selected = entry.get("source_range", {})
        reference_identity = (slot, identity, selected.get("start"), selected.get("end"),
            entry.get('time_seconds') if slot=='guides' else None)
        if reference_identity in seen:
            ref["reason"] = "同一输入区域的素材已发送，不重复发送"
            continue
        anchor = slot in {"first_frame", "last_frame"}
        guide = slot=='guides'
        if guide and counters.get(slot,0)>=8:
            ref['reason']='时间锚点最多8个'
        if mode == "ref" and not (native_roles and (anchor or guide)):
            maximum = (limits or {}).get("max_" + slot)
            all_max = (limits or {}).get("max_total_files")
            if type(maximum) is not int or type(all_max) is not int:
                ref["reason"] = "当前参考输入范围尚未读取"
                if native_roles:
                    issues.append(ref['reason'] + '：' + ref['entity_id'])
                continue
            if counters.get(slot, 0) >= maximum or total >= all_max:
                ref["reason"] = "当前能力数量上限；按人物、场景、道具、附加素材优先级未发送"
                if native_roles:
                    issues.append('参考素材超过当前能力数量上限，请调整素材后重新预检：' + ref['entity_id'])
                continue
            duration = entry.get("source_range", {}).get("end", 0) - entry.get("source_range", {}).get("start", 0)
            if slot in durations:
                maximum_seconds = limits.get("max_total_" + {"videos": "video", "audios": "audio"}[slot] + "_duration")
                if type(maximum_seconds) in (int, float) and durations[slot] + duration > maximum_seconds + .01:
                    ref["reason"] = "当前能力总参考时长上限；按优先级未发送"
                    if native_roles:
                        issues.append('参考素材超过当前能力总时长上限，请调整选段后重新预检：' + ref['entity_id'])
                    continue
                durations[slot] += duration
        if ref["reason"]:
            issues.append(ref["reason"] + "：" + ref["entity_id"])
            continue
        ref["sent"] = True
        seen.add(reference_identity)
        counters[slot] = counters.get(slot, 0) + 1
        if not anchor and not guide:
            total += 1
        if slot in {"first_frame", "last_frame"}:
            inputs[slot] = entry
        else:
            inputs[slot].append(entry)
    if mode == "ref" and not (any(inputs[key] for key in ("images", "videos", "audios"))
            or native_roles and (inputs['first_frame'] or inputs['last_frame'] or inputs['guides'])):
        issues.append("全能参考至少需要一份当前可发送的素材")
    draft = {"recipe_id": recipe["id"] if recipe else chosen, "prompt": prompt,
        "controls": copy.deepcopy(h3.get("controls", {})), "inputs": inputs,
        "storyboard": {"prompt_parts": parts, "references": [ref for ref, _ in references],
            "notes": list(dict.fromkeys(notes)), "limits": limits,
            "previous_shot_id": previous["id"] if previous else None}}
    if profile_id is not None:
        draft["deployment_profile_id"] = profile_id
    return draft, list(dict.fromkeys(issues))
