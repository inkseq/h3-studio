"""Translate saved single-shot drafts to the same H3 contract used by the UI.

Document editing is pure; receipt reads and CPU derivation are supplied by the
caller. No model execution, provider lookup or new source of project state.
"""

from __future__ import annotations

import copy
import math
import uuid

from .capabilities import RECIPES, LIMITS, control_schema as base_control_schema
from .repository import Conflict, NotFound

SLOTS = {
    "images": "image",
    "videos": "video",
    "audios": "audio",
    "first_frame": "image",
    "last_frame": "image",
}
ROLES = {
    "images": {"reference", "identity"},
    "videos": {"reference", "motion"},
    "audios": {"reference", "audio"},
    "first_frame": {"firstFrame"},
    "last_frame": {"lastFrame"},
}
DEFAULT_ROLE = {
    "images": "reference",
    "videos": "motion",
    "audios": "audio",
    "first_frame": "firstFrame",
    "last_frame": "lastFrame",
}


def control_schema():
    """Editable union only; the selected immutable compiler validates execution."""
    from .inference.wangp_profile_dispatch import control_schema as profile_schema

    result = base_control_schema()
    native = profile_schema('h3-pruned-rank8-int8-sdpa-p4-29gib-controls-v6', 'ref')
    result.update(
        {
            key: copy.deepcopy(value)
            for key, value in native.items()
            if key not in result
        }
    )
    for key, schema in native.items():
        if 'enum' in schema and 'enum' in result[key]:
            result[key]['enum'] = list(
                dict.fromkeys([*result[key]['enum'], *schema['enum']])
            )
    result['shift_video']['maximum'] = max(
        result['shift_video']['maximum'], native['shift_video']['maximum']
    )
    # Confirmed preview receipts, never editable card/draft controls.
    result.pop('script_window_prompts', None)
    return result


def fields(value, allowed, label):
    if not isinstance(value, dict) or set(value) - set(allowed):
        raise ValueError(label + "包含不支持的字段或不是对象")
    return value


def shot_entity(project, shot_id):
    shot = next(
        (e for e in project["entities"] if e["id"] == shot_id and e["type"] == "shot"),
        None,
    )
    if shot is None:
        raise NotFound("shot_not_found")
    return shot


def validate_controls_patch(controls):
    schemas = control_schema()
    fields(controls, set(schemas) - {"guides", "video_audio"}, "生成控制")
    for key, value in controls.items():
        schema = schemas[key]
        if schema.get("available") is False:
            raise ValueError(key + "尚不可用")
        types = schema.get("type")
        types = types if isinstance(types, list) else [types]
        if value is None and "null" in types:
            continue
        valid = (
            ("string" in types and isinstance(value, str))
            or ("integer" in types and type(value) is int)
            or (
                "number" in types
                and type(value) in (int, float)
                and math.isfinite(value)
            )
            or ("boolean" in types and type(value) is bool)
            or ("array" in types and isinstance(value, list))
        )
        if not valid or "enum" in schema and value not in schema["enum"]:
            raise ValueError(key + "格式或取值无效")
        if type(value) in (int, float) and (
            value < schema.get("minimum", -math.inf)
            or value > schema.get("maximum", math.inf)
        ):
            raise ValueError(key + "超出模型控制范围")
        if key == "seed" and (
            not value.isascii()
            or not value.isdecimal()
            or len(value) > 20
            or int(value) > 18446744073709551615
        ):
            raise ValueError("seed须为uint64十进制字符串或null")
        if key in {'video_excerpts', 'audio_excerpts'}:
            if len(value) > 3:
                raise ValueError('wangp_invalid_excerpts')
            for entry in value:
                if (
                    not isinstance(entry, dict)
                    or set(entry) != {'time_seconds', 'duration_seconds'}
                    or any(
                        type(v) not in (int, float) or not math.isfinite(v)
                        for v in entry.values()
                    )
                    or entry['time_seconds'] < 0
                    or not 2 <= entry['duration_seconds'] <= 15
                ):
                    raise ValueError('wangp_invalid_excerpts')


def validate_range(value, source=None):
    if value is None:
        return
    fields(value, {"start", "end"}, "原文件选段")
    start, end = value.get("start"), value.get("end")
    if (
        any(type(x) not in (int, float) or not math.isfinite(x) for x in (start, end))
        or start < 0
        or not 2 <= end - start <= 15
    ):
        raise ValueError("选段须有明确start/end，且长度为2–15秒")
    if source:
        duration = source.get("data", {}).get("metadata", {}).get("source_duration")
        if (
            source["type"] not in {"audio", "video"}
            or type(duration) not in (int, float)
            or end > duration + 0.01
        ):
            raise ValueError("选段须在已验证原视频或音频时长内")


def input_entries(inputs, *, allow_duplicates=False):
    """Validate editable syntax without requiring a complete executable request."""
    fields(inputs, {*SLOTS, "guides"}, "生成输入")
    entries = []
    for slot, values in inputs.items():
        single = slot in {"first_frame", "last_frame"}
        if single:
            values = [] if values is None else [values]
        elif not isinstance(values, list) or len(values) > (
            8 if slot == "guides" else LIMITS["max_" + slot]
        ):
            raise ValueError(slot + "必须为模型数量范围内的数组")
        seen = set()
        for entry in values:
            guide = slot == "guides"
            allowed = (
                {"media_id", "time_seconds", "use_audio", "source_range"}
                if guide
                else (
                    {"asset_id"}
                    if single
                    else {
                        "asset_id",
                        "purpose",
                        "source_range",
                        *(["include_audio"] if slot == "videos" else []),
                    }
                )
            )
            fields(entry, allowed, slot)
            ident = entry.get("media_id" if guide else "asset_id")
            if not isinstance(ident, str) or not 1 <= len(ident) <= 160:
                raise ValueError(
                    "素材须使用上传返回的asset_id；锚点media_id也使用该收据ID"
                )
            role = entry.get("purpose", DEFAULT_ROLE.get(slot))
            if not guide and role not in ROLES[slot]:
                raise ValueError(slot + "的purpose不匹配素材用途")
            if not guide and ident in seen and not allow_duplicates:
                raise ValueError("同一输入区域不能重复同一素材")
            seen.add(ident)
            for key in ("include_audio", "use_audio"):
                if key in entry and type(entry[key]) is not bool:
                    raise ValueError(key + "必须为布尔值")
            if guide and (
                type(entry.get("time_seconds")) not in (int, float)
                or not math.isfinite(entry["time_seconds"])
                or not 0 <= entry["time_seconds"] <= 360000
            ):
                raise ValueError("锚点需要非负time_seconds")
            validate_range(entry.get("source_range"))
            entries.append((slot, entry, ident))
    return entries


def prepare_patch(action, resolve):
    if action.get("deployment_profile_id") is not None:
        from .runtime_catalog import get_profile

        get_profile(action["deployment_profile_id"])
    if "recipe_id" in action and (
        not isinstance(action["recipe_id"], str) or action["recipe_id"] not in RECIPES
    ):
        raise ValueError("未支持的生成recipe_id")
    if "prompt" in action and (
        not isinstance(action["prompt"], str) or len(action["prompt"]) > 12000
    ):
        raise ValueError("提示词须为不超过12000字符的文本；空文本可以保存草稿")
    if "controls" in action:
        validate_controls_patch(action["controls"])
    sources = {}
    for slot, entry, ident in input_entries(action.get("inputs", {})):
        if ident not in sources:
            sources[ident] = resolve(ident)
        source = sources[ident]
        if slot != "guides" and source["type"] != SLOTS[slot]:
            raise ValueError(slot + "的素材类型不匹配")
        if slot == "guides" and source["type"] == "image" and entry.get("use_audio"):
            raise ValueError("图片锚点不能包含声音")
        validate_range(entry.get("source_range"), source)
    return sources


def configure(project, action, sources):
    shot = shot_entity(project, action["shot_id"])
    h3 = shot["data"].setdefault("h3", {})
    if "recipe_id" in action:
        h3["recipeId"] = action["recipe_id"]
    if "deployment_profile_id" in action:
        h3["deploymentProfileId"] = action["deployment_profile_id"]
    if "prompt" in action:
        shot["data"]["prompt"] = action["prompt"]
    if "controls" in action:
        saved_controls = fields(
            h3.get("controls", {}), control_schema(), "已保存的生成控制"
        )
        h3["controls"] = {**saved_controls, **copy.deepcopy(action["controls"])}
    entities = {}
    for ident, source in sources.items():
        entity = next(
            (e for e in project["entities"] if e["data"].get("cloudAssetId") == ident),
            None,
        )
        if entity is None:
            entity = {
                **copy.deepcopy(source),
                "id": "asset-" + uuid.uuid5(uuid.NAMESPACE_URL, ident).hex,
                "parentId": None,
                "description": "",
                "version": 1,
                "status": "draft",
                "order": len([e for e in project["entities"] if e["parentId"] is None]),
            }
            project["entities"].append(entity)
        if (
            entity["type"] != source["type"]
            or not entity["data"].get("fileId")
            or entity["data"].get("missingFile")
        ):
            raise Conflict("draft_asset_binding_changed")
        entities[ident] = entity
    lookup = {e["id"]: e for e in project["entities"]}
    inputs = action.get("inputs", {})
    for slot in SLOTS:
        if slot not in inputs:
            continue
        old = [
            link
            for link in project["links"]
            if link["target"] == shot["id"]
            and link["role"] in ROLES[slot]
            and lookup[link["source"]]["type"] == SLOTS[slot]
        ]
        entries = [entry for field, entry, _ in input_entries({slot: inputs[slot]})]
        kept = []
        ranges = shot["data"].setdefault("referenceRanges", {})
        for entry in entries:
            entity = entities[entry["asset_id"]]
            previous_link = next((v for v in old if v["source"] == entity["id"]), None)
            role = entry.get(
                "purpose",
                previous_link["role"] if previous_link else DEFAULT_ROLE[slot],
            )
            link = next(
                (v for v in old if v["source"] == entity["id"] and v["role"] == role),
                None,
            )
            if link is None and previous_link:
                link = {**previous_link, "role": role}
            if link is None:
                link = {
                    "id": "link-" + uuid.uuid4().hex,
                    "source": entity["id"],
                    "target": shot["id"],
                    "role": role,
                }
            kept.append(link)
            if "source_range" in entry:
                if entry["source_range"] is None:
                    ranges.pop(link["id"], None)
                else:
                    ranges[link["id"]] = {
                        **entry["source_range"],
                        "fileId": entity["data"]["fileId"],
                    }
            if slot == "videos" and "include_audio" in entry:
                h3.setdefault("video_audio", {})[entity["id"]] = entry["include_audio"]
        old_ids, kept_ids = {v["id"] for v in old}, {v["id"] for v in kept}
        # Preserve unrelated edge ordering and stable IDs. In-slot reorder follows
        # the explicit list, while a patch to an unchanged slot stays unchanged.
        replacement = iter(kept)
        links = []
        for link in project["links"]:
            if link["id"] not in old_ids:
                links.append(link)
            else:
                substitute = next(replacement, None)
                if substitute:
                    links.append(substitute)
        project["links"] = links + list(replacement)
        for ident in old_ids - kept_ids:
            ranges.pop(ident, None)
    if "guides" in inputs:
        guides = []
        for entry in inputs["guides"]:
            entity = entities[entry["media_id"]]
            previous = next(
                (
                    g
                    for g in h3.get("guides", [])
                    if g.get("media_id") == entity["id"]
                    and g.get("time_seconds") == entry["time_seconds"]
                ),
                {},
            )
            guide = {
                "media_id": entity["id"],
                "time_seconds": entry["time_seconds"],
                "use_audio": entry.get(
                    "use_audio", previous.get("use_audio", entity["type"] == "audio")
                ),
            }
            if "source_range" in entry:
                if entry["source_range"] is not None:
                    guide["source_range"] = {
                        **entry["source_range"],
                        "fileId": entity["data"]["fileId"],
                    }
            elif "source_range" in previous:
                guide["source_range"] = copy.deepcopy(previous["source_range"])
            guides.append(guide)
        h3["guides"] = guides
    shot["version"] += 1
    shot["status"] = "review"


def selected_recipe(recipes, h3):
    """Mirror the browser's explicit recipe, input mode, then default order."""
    if h3.get("recipeId"):
        return next(
            (recipe for recipe in recipes if recipe["id"] == h3["recipeId"]), None
        )
    if h3.get("inputMode"):
        return next(
            (recipe for recipe in recipes if recipe["mode"] == h3["inputMode"]), None
        )
    return next(iter(recipes), None)


def shot_location_ids(project, shot, lookup):
    """Shot location overrides its scene default; explicit location edges add."""
    scene = lookup.get(shot.get("parentId"), {})
    bound = shot.get("data", {}).get("locationId") or scene.get("data", {}).get(
        "locationId"
    )
    identities = ([bound] if bound else []) + [
        link["source"]
        for link in project["links"]
        if link["target"] == shot["id"]
        and link["role"] in {"location", "reference"}
        and lookup.get(link["source"], {}).get("type") == "location"
    ]
    if any(not isinstance(ident, str) for ident in identities):
        raise ValueError("地点绑定格式无效，请重新选择")
    return list(dict.fromkeys(identities))


def read_draft(project, shot_id, *, recipes=None, capabilities=None):
    shot = shot_entity(project, shot_id)
    if "storyboard" in shot["data"]:
        from .storyboard import compile_draft

        return compile_draft(project, shot, recipes=recipes, capabilities=capabilities)
    h3 = shot["data"].get("h3", {})
    available = (
        recipes
        if recipes is not None
        else [{"id": key, "mode": mode} for key, mode in RECIPES.items()]
    )
    recipe = selected_recipe(available, h3)
    lookup = {e["id"]: e for e in project["entities"]}
    inputs = {
        "images": [],
        "videos": [],
        "audios": [],
        "first_frame": None,
        "last_frame": None,
        "guides": [],
    }
    issues = []
    if recipe is None:
        issues.append("草稿配方已不可用，请明确选择后重新预检")
    ranges = shot["data"].get("referenceRanges", {})
    video_audio = h3.get("video_audio", {})
    if not isinstance(ranges, dict):
        issues.append("参考选段格式无效，请重新确认")
        ranges = {}
    if not isinstance(video_audio, dict):
        issues.append("参考视频原声配置格式无效")
        video_audio = {}
    if not isinstance(h3.get("controls", {}), dict):
        issues.append("保存的生成控制格式无效")

    def entry_for(entity, selected=None):
        if not entity or entity["type"] not in {"image", "video", "audio"}:
            issues.append("参考必须指向实际图片、视频或音频")
            return None
        data = entity["data"]
        ident = data.get("cloudAssetId")
        if not ident or not data.get("fileId") or data.get("missingFile"):
            issues.append("素材尚未同步为可用的云端输入：" + entity["id"])
            return None
        result = {"asset_id": ident}
        if selected is not None:
            if not isinstance(selected, dict):
                issues.append("参考选段格式无效：" + entity["id"])
                return result
            if selected.get("fileId") != data["fileId"]:
                issues.append("原素材文件已改变，请重新确认选段：" + entity["id"])
            value = {k: selected.get(k) for k in ("start", "end")}
            try:
                validate_range(value)
            except ValueError as error:
                issues.append(str(error))
            result["source_range"] = value
        return result

    references = []
    for link in project["links"]:
        if link["target"] != shot_id:
            continue
        entity = lookup.get(link["source"])
        if entity and entity["type"] in {"image", "video", "audio"}:
            references.append((entity, link["role"], ranges.get(link["id"])))
        elif link["role"] == "dependency":
            issues.append("前置步骤尚未明确选择为实际参考素材")
    # Cast/location assignments remain story data in FL. Only REF turns their
    # uploaded images into model inputs; explicit media edges stay explicit.
    if recipe and recipe["mode"] == "ref" and not h3.get("explicitReferences"):
        scene = lookup.get(shot["parentId"], {})
        cast = lambda e: [
            v for v in e.get("data", {}).get("cast", []) if isinstance(v, dict)
        ]
        own, inherited = cast(shot), cast(scene)
        if h3.get("explicitCast"):
            inherited = []
        characters = list(
            dict.fromkeys(
                [v.get("characterId") for v in [*inherited, *own]]
                + [
                    v["source"]
                    for v in project["links"]
                    if v["target"] == shot_id
                    and v["role"] == "identity"
                    and lookup.get(v["source"], {}).get("type") == "character"
                ]
            )
        )
        for ident in (x for x in characters if isinstance(x, str)):
            character = lookup.get(ident, {})
            binding = next(
                (v for v in own if v.get("characterId") == ident), None
            ) or next((v for v in inherited if v.get("characterId") == ident), {})
            look = next(
                (
                    v
                    for v in character.get("data", {}).get("looks", [])
                    if v.get("id") == binding.get("lookId")
                ),
                None,
            )
            if not look or not any(look.get("gallery", {}).values()):
                issues.append("角色尚未选择可用造型和参考图：" + ident)
                continue
            for asset_id in filter(None, look.get("gallery", {}).values()):
                if not any(e and e["id"] == asset_id for e, _, _ in references):
                    references.append((lookup.get(asset_id), "identity", None))
        try:
            location_ids = shot_location_ids(project, shot, lookup)
        except ValueError as error:
            issues.append(str(error))
            location_ids = []
        for ident in location_ids:
            location = lookup.get(ident)
            if not location or location["type"] != "location":
                issues.append("已绑定地点不存在，请重新选择")
                continue
            asset_ids = location["data"].get("referenceAssetIds", [])
            if not isinstance(asset_ids, list) or any(
                not isinstance(asset_id, str) for asset_id in asset_ids
            ):
                issues.append("地点参考图格式无效，请重新选择")
                continue
            for asset_id in asset_ids:
                asset = lookup.get(asset_id)
                if asset is not None and asset["type"] != "image":
                    issues.append("地点参考请选择已上传的图片")
                    continue
                if not any(e and e["id"] == asset_id for e, _, _ in references):
                    references.append((asset, "reference", None))
    for entity, role, selected in references:
        value = entry_for(entity, selected)
        if value is None:
            continue
        slot = (
            "first_frame"
            if role == "firstFrame"
            else "last_frame"
            if role == "lastFrame"
            else {"image": "images", "video": "videos", "audio": "audios"}[
                entity["type"]
            ]
        )
        if slot in {"first_frame", "last_frame"}:
            if inputs[slot] is not None:
                issues.append("同一首尾帧区域存在多个关联，请明确保留一个")
            inputs[slot] = value
        else:
            value["purpose"] = role
            if slot == "videos":
                value["include_audio"] = video_audio.get(entity["id"]) is not False
            inputs[slot].append(value)
    for guide in h3.get("guides", []):
        if not isinstance(guide, dict):
            issues.append("时间锚点格式无效")
            continue
        value = entry_for(lookup.get(guide.get("media_id")), guide.get("source_range"))
        if value is not None:
            value["media_id"] = value.pop("asset_id")
            value.update(
                time_seconds=guide.get("time_seconds"),
                use_audio=guide.get("use_audio", False),
            )
            inputs["guides"].append(value)
    draft = {
        "recipe_id": recipe["id"] if recipe else h3.get("recipeId"),
        "prompt": shot["data"].get("prompt", shot.get("description", "")),
        "controls": copy.deepcopy(h3.get("controls", {})),
        "inputs": inputs,
    }
    if h3.get("deploymentProfileId") is not None:
        draft["deployment_profile_id"] = h3["deploymentProfileId"]
    return draft, list(dict.fromkeys(issues))


def plan_body(project, shot_id, capabilities, derive, *, validate_frame=None):
    draft, issues = read_draft(
        project, shot_id, recipes=capabilities["recipes"], capabilities=capabilities
    )
    if issues:
        raise ValueError("；".join(issues))
    recipe = next(
        (r for r in capabilities["recipes"] if r["id"] == draft["recipe_id"]), None
    )
    if not recipe:
        raise ValueError("草稿配方已不可用，请明确选择后重新预检")
    if draft.get("deployment_profile_id") is not None:
        from .runtime_catalog import get_profile
        from .inference.wangp_profile_dispatch import control_schema as profile_schema

        get_profile(draft["deployment_profile_id"])
        recipe = {
            **recipe,
            "controls": profile_schema(draft["deployment_profile_id"], recipe["mode"]),
            "deployment_preset": {},
        }
    if not isinstance(draft["prompt"], str) or not draft["prompt"].strip():
        raise ValueError("先写清这个镜头的画面与动作")
    shot = shot_entity(project, shot_id)
    storyboard = shot["data"].get("storyboard")
    from .h3_profile_support import full_controls

    native_anchors = bool(
        draft.get("deployment_profile_id")
        and full_controls(draft["deployment_profile_id"])
    )
    if (
        storyboard
        and (storyboard["mode"] == "fl" or native_anchors)
        and storyboard.get("continuePrevious")
    ):
        from .storyboard import previous_shot

        previous = previous_shot(project, shot)
        lookup = {entity["id"]: entity for entity in project["entities"]}
        selected = lookup[previous["data"]["selectedAssetId"]]
        image = lookup[storyboard["previousFrame"]["frameAssetId"]]
        if validate_frame is None:
            raise ValueError("上一镜末帧必须由服务端核对来源")
        validate_frame(
            image["data"]["cloudAssetId"], selected["data"]["cloudArtifactId"]
        )
    saved = draft["controls"]
    fields(saved, control_schema(), "已保存的生成控制")
    # Changing engines must not silently discard a user's explicit controls.
    # Keep the draft unchanged so the caller can choose compatible settings.
    fields(saved, recipe["controls"], "当前配方不支持的已保存生成控制")
    controls = {}
    preset = recipe.get("deployment_preset", {})
    for key, schema in recipe["controls"].items():
        if schema.get("available") is False or key in {"guides", "video_audio"}:
            continue
        if key in saved and saved[key] is not None and saved[key] != "":
            controls[key] = saved[key]
        elif (
            key not in saved
            and preset.get("applies_to") == "unset_controls_only"
            and key in {"encoder_device", "video_decode"}
            and preset.get("controls", {}).get(key) in schema.get("enum", [])
        ):
            controls[key] = preset["controls"][key]
        elif key == "duration":
            controls[key] = shot["data"].get("seconds", 5)
        elif "default" in schema:
            controls[key] = schema["default"]
    # The browser can retain null while a numeric input is cleared. Validate the
    # resolved controls, after applying the same defaults, not that UI draft.
    validate_controls_patch(controls)
    inputs = copy.deepcopy(draft["inputs"])
    input_entries(inputs, allow_duplicates=True)
    full_native = False
    if draft.get('deployment_profile_id'):
        from .h3_profile_support import full_controls

        full_native = full_controls(draft['deployment_profile_id'])
    if (
        not full_native
        and recipe["mode"] == "fl"
        and any(inputs[k] for k in ("images", "videos", "audios"))
    ):
        raise ValueError("首尾帧配方不能混用全能参考；请明确切换配方或解除关联")
    if (
        not full_native
        and recipe["mode"] == "ref"
        and (inputs["first_frame"] or inputs["last_frame"])
    ):
        raise ValueError("全能参考配方不接受首尾帧约束；原素材仍保留，请明确更改关联")
    if (
        recipe["mode"] == "ref"
        and not any(inputs[k] for k in ("images", "videos", "audios"))
        and not (
            full_native
            and (inputs['first_frame'] or inputs['last_frame'] or inputs.get('guides'))
        )
    ):
        raise ValueError("全能参考至少需要一份参考素材")
    cache = {}
    for slot, entries in inputs.items():
        for entry in (
            []
            if entries is None
            else [entries]
            if isinstance(entries, dict)
            else entries
        ):
            selected = entry.pop("source_range", None)
            if selected:
                field = "media_id" if slot == "guides" else "asset_id"
                key = (entry[field], selected["start"], selected["end"])
                if key not in cache:
                    cache[key] = derive(*key)
                entry[field] = cache[key]
    # The browser keeps the first ordinary reference to a model asset, including
    # when a character gallery and a direct link resolve to the same receipt.
    seen = set()
    for slot in ("images", "videos", "audios"):
        unique = []
        for entry in inputs[slot]:
            if entry["asset_id"] not in seen:
                seen.add(entry["asset_id"])
                unique.append(entry)
        inputs[slot] = unique
    for slot in ("first_frame", "last_frame"):
        if inputs[slot] is None:
            del inputs[slot]
    lookup = {e["id"]: e for e in project["entities"]}
    scene = lookup[shot["parentId"]]
    return {
        "client_ref": {
            "project_id": project["id"],
            "shot_id": shot_id,
            "shot_version": shot["version"],
            "scene_id": scene["id"],
            "chapter_id": scene["parentId"],
        },
        "recipe_id": recipe["id"],
        "capabilities_version": capabilities["capabilities_version"],
        **(
            {"deployment_profile_id": draft["deployment_profile_id"]}
            if draft.get("deployment_profile_id")
            else {}
        ),
        "prompt": draft["prompt"].strip(),
        "controls": controls,
        "inputs": inputs,
        "client_edit": {"edit_duration_s": shot["data"].get("seconds", 5)},
    }
