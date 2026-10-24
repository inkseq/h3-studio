"""Bounds and structural integrity for cloud copies of Yingxu v4 projects."""

from __future__ import annotations

import json
import math
import re

ID = re.compile(r"[A-Za-z0-9_-]{1,160}$")
TYPES = {
    "chapter",
    "scene",
    "shot",
    "character",
    "location",
    "prop",
    "image",
    "audio",
    "video",
    "note",
    "generation",
}
ROLES = {
    "identity": {"character", "image"},
    "location": {"location"},
    "firstFrame": {"image"},
    "lastFrame": {"image"},
    "motion": {"video"},
    "audio": {"audio"},
    "reference": {"image", "video", "audio", "character", "location", "prop", "note"},
    "dependency": {"shot", "generation"},
}


def validate_generated_audio_binding(value):
    """Structural editing data only; authorization belongs to render planning."""
    if (
        not isinstance(value, dict)
        or set(value)
        != {"jobId", "videoEntityId", "videoArtifactId", "audioArtifactId"}
        or any(not isinstance(v, str) or not ID.fullmatch(v) for v in value.values())
    ):
        raise ValueError("生成声音关联格式无效")
    return value


def validate_project(project, max_bytes=8 * 1024 * 1024):
    def check_plain(value, depth=0):
        if depth > 24:
            raise ValueError("项目数据层级过深")
        if isinstance(value, dict):
            if len(value) > 20000 or any(
                k in {"__proto__", "constructor", "prototype"} for k in value
            ):
                raise ValueError("项目包含非法键")
            for v in value.values():
                check_plain(v, depth + 1)
        elif isinstance(value, list):
            if len(value) > 20000:
                raise ValueError("项目列表过大")
            for v in value:
                check_plain(v, depth + 1)
        elif isinstance(value, float) and not math.isfinite(value):
            raise ValueError("项目数值无效")
        elif isinstance(value, str) and len(value) > 200000:
            raise ValueError("项目文本过长")
        elif not isinstance(value, (str, int, float, bool, type(None))):
            raise ValueError("项目必须为普通JSON数据")

    check_plain(project)
    if not isinstance(project, dict) or project.get("schemaVersion") != 4:
        raise ValueError("仅支持映序v4项目")
    if (
        len(json.dumps(project, ensure_ascii=False, allow_nan=False).encode())
        > max_bytes
    ):
        raise ValueError("项目数据过大")
    if not isinstance(project.get("id"), str) or not ID.fullmatch(project["id"]):
        raise ValueError("项目ID无效")
    title = project.get("title")
    if not isinstance(title, str) or not title.strip() or len(title) > 160:
        raise ValueError("项目标题须为1至160字符")
    if not isinstance(project.get("logline"), str) or len(project["logline"]) > 24000:
        raise ValueError("项目简介无效")
    journey = project.get("journey", {})
    if not isinstance(journey, dict):
        raise ValueError("项目制作流程必须为对象")
    for name in ("brief", "sound", "soundTracks", "captionTracks"):
        if name in journey and not isinstance(journey[name], dict):
            raise ValueError("项目制作流程字段格式无效")
    for tracks in journey.get("soundTracks", {}).values():
        if not isinstance(tracks, list):
            raise ValueError("声音轨道格式无效")
        for track in tracks:
            if not isinstance(track, dict):
                raise ValueError("声音轨道格式无效")
            from .render_timing import number, validate_track_fields

            validate_track_fields(track)
            if "needsReview" in track and type(track["needsReview"]) is not bool:
                raise ValueError("声音待确认标记无效")
            if track.get("generatedFrom") is not None:
                validate_generated_audio_binding(track["generatedFrom"])
                if track.get("detached") is True:
                    if track.get("shotId") != "" or not number(
                        track.get("offset", 0), 0, 3600
                    ):
                        raise ValueError("分离原声必须使用独立绝对起点")
                elif (
                    not isinstance(track.get("shotId"), str)
                    or not ID.fullmatch(track["shotId"])
                    or type(track.get("offset", 0)) not in (int, float)
                    or track.get("offset", 0) != 0
                ):
                    raise ValueError("生成声音必须关联镜头并从镜头起点播放")
                # Deletion/replacement may leave stale IDs: keep the draft for
                # undo or explicit detachment; rendering checks live bindings.
    entities, links = project.get("entities"), project.get("links")
    if (
        not isinstance(entities, list)
        or len(entities) > 5000
        or not isinstance(links, list)
        or len(links) > 20000
    ):
        raise ValueError("项目节点或连线超限")
    lookup = {}
    for e in entities:
        if (
            not isinstance(e, dict)
            or not isinstance(e.get("id"), str)
            or not ID.fullmatch(e["id"])
            or e["id"] in lookup
            or not isinstance(e.get("type"), str)
            or e["type"] not in TYPES
            or not isinstance(e.get("data"), dict)
            or type(e.get("version")) is not int
            or e["version"] < 1
        ):
            raise ValueError("节点ID、类型、版本或内容无效")
        if not isinstance(e.get("description"), str) or len(e["description"]) > 24000:
            raise ValueError("节点描述无效")
        if (
            not isinstance(e.get("title"), str)
            or len(e["title"]) > 160
            or type(e.get("order")) is not int
            or e["order"] < 0
            or not isinstance(e.get("status"), str)
            or e["status"] not in {"draft", "review", "ready"}
            or e.get("parentId") is not None
            and not isinstance(e["parentId"], str)
        ):
            raise ValueError("节点标题、顺序、状态或父级无效")
        data = e["data"]
        if "locationId" in data and (
            e["type"] not in {"scene", "shot"}
            or not isinstance(data["locationId"], str)
            or data["locationId"]
            and not ID.fullmatch(data["locationId"])
        ):
            raise ValueError("地点绑定无效")
        if "libraryFileIds" in data and (
            e["type"] not in {"character", "location", "prop"}
            or not isinstance(data["libraryFileIds"], list)
            or any(not isinstance(ident, str) for ident in data["libraryFileIds"])
            or len(set(data["libraryFileIds"])) != len(data["libraryFileIds"])
        ):
            raise ValueError("Invalid profile filing references. / 档案文件引用无效。")
        if "referenceAssetIds" in data and (
            e["type"] != "location"
            or not isinstance(data["referenceAssetIds"], list)
            or len(data["referenceAssetIds"]) > 100
            or any(
                not isinstance(v, str) or not ID.fullmatch(v)
                for v in data["referenceAssetIds"]
            )
            or len(set(data["referenceAssetIds"])) != len(data["referenceAssetIds"])
        ):
            raise ValueError("地点参考图列表无效")
        for name in ("cloudAssetId", "cloudArtifactId", "fileId"):
            if (
                name in data
                and data[name] is not None
                and (not isinstance(data[name], str) or len(data[name]) > 200)
            ):
                raise ValueError("素材文件身份无效")
        for name in ("h3",):
            if name in data and not isinstance(data[name], dict):
                raise ValueError("H3控制必须为对象")
        if "cast" in data:
            if not isinstance(data["cast"], list) or len(data["cast"]) > 100:
                raise ValueError("角色列表无效")
            actor_ids = set()
            for cast in data["cast"]:
                # CharacterStudio binds a character AND an explicit look. Older
                # drafts may still contain bare character IDs; preserve them.
                if isinstance(cast, str):
                    actor_id = cast
                elif (
                    isinstance(cast, dict)
                    and set(cast) == {"characterId", "lookId"}
                    and isinstance(cast.get("lookId"), str)
                    and len(cast["lookId"]) <= 160
                ):
                    actor_id = cast.get("characterId")
                else:
                    raise ValueError("角色造型绑定无效")
                if (
                    not isinstance(actor_id, str)
                    or not ID.fullmatch(actor_id)
                    or actor_id in actor_ids
                ):
                    raise ValueError("角色列表包含重复或无效身份")
                actor_ids.add(actor_id)
        if "looks" in data:
            if (
                e["type"] != "character"
                or not isinstance(data["looks"], list)
                or len(data["looks"]) > 100
            ):
                raise ValueError("角色造型列表无效")
            look_ids = set()
            for look in data["looks"]:
                if (
                    not isinstance(look, dict)
                    or not isinstance(look.get("id"), str)
                    or not ID.fullmatch(look["id"])
                    or look["id"] in look_ids
                    or not isinstance(look.get("name"), str)
                    or len(look["name"]) > 160
                    or not isinstance(look.get("description", ""), str)
                    or len(look.get("description", "")) > 24000
                    or type(look.get("version")) is not int
                    or look["version"] < 1
                    or not isinstance(look.get("gallery"), dict)
                    or set(look["gallery"]) - {"front", "side", "full"}
                    or any(
                        not isinstance(value, str) or len(value) > 160
                        for value in look["gallery"].values()
                    )
                ):
                    raise ValueError("角色造型内容无效")
                look_ids.add(look["id"])
        if "seconds" in data and (
            type(data["seconds"]) not in (int, float) or not 0 < data["seconds"] <= 3600
        ):
            raise ValueError("镜头或素材时长无效")
        if "selectedVideoRange" in data and data["selectedVideoRange"] is not None:
            selected = data["selectedVideoRange"]
            if (
                e["type"] != "shot"
                or not isinstance(selected, dict)
                or set(selected)
                not in (
                    {
                        "assetId",
                        "fileId",
                        "cloudAssetId",
                        "cloudArtifactId",
                        "start",
                        "end",
                    },
                    {
                        "assetId",
                        "fileId",
                        "cloudAssetId",
                        "cloudArtifactId",
                        "start",
                        "end",
                        "playback_rate",
                    },
                )
                or not isinstance(selected.get("assetId"), str)
                or not ID.fullmatch(selected["assetId"])
                or any(
                    selected.get(k) is not None
                    and (
                        not isinstance(selected[k], str)
                        or not 1 <= len(selected[k]) <= 200
                    )
                    for k in ("fileId", "cloudAssetId", "cloudArtifactId")
                )
                or any(
                    type(selected.get(k)) not in (int, float)
                    or not 0 <= selected[k] <= 360000
                    for k in ("start", "end")
                )
                or selected["end"] <= selected["start"]
                or "playback_rate" in selected
                and (
                    type(selected["playback_rate"]) not in (int, float)
                    or selected["playback_rate"] not in (0.5, 0.75, 1, 1.25, 1.5, 2)
                )
            ):
                raise ValueError("已采用视频的选段格式无效")
            # Media replacement/deletion may leave a stale recoverable edit.
            # Save that draft; the render compiler blocks stale bindings.
        from .shot_segments import validate_segments_data

        validate_segments_data(e)
        if "guides" in data.get("h3", {}) and (
            not isinstance(data["h3"]["guides"], list) or len(data["h3"]["guides"]) > 8
        ):
            raise ValueError("时间锚点列表无效")
        if e["type"] == "generation" and (
            not isinstance(data.get("recipe"), str)
            or data["recipe"] not in {"video", "image", "music"}
        ):
            raise ValueError("生成步骤配方无效")
        for name in (
            "prompt",
            "goal",
            "look",
            "role",
            "time",
            "props",
            "age",
            "source",
            "referenceRole",
            "worldStatus",
            "script",
        ):
            if name in data and (
                not isinstance(data[name], str)
                or len(data[name]) > (120000 if name == "script" else 24000)
            ):
                raise ValueError("节点内容字段无效")
        lookup[e["id"]] = e
    from .storyboard import validate as validate_storyboard

    validate_storyboard(project, lookup)
    # These are editable captions, not an execution/approval authority. A
    # timeline edit may leave old cues outside the new chapter or overlapping;
    # keep that recoverable draft. The UI gates confirmation/SRT export; the
    # server independently reconstructs confirmation and burn-only bounds when
    # compiling a render, never from a caller's boolean or arbitrary font/path.
    for chapter_id, track in journey.get("captionTracks", {}).items():
        if (
            chapter_id not in lookup
            or lookup[chapter_id]["type"] != "chapter"
            or not isinstance(track, dict)
        ):
            raise ValueError("字幕章节或字幕轨道无效")
        cues = track.get("cues")
        if not isinstance(cues, list) or len(cues) > 500:
            raise ValueError("每章字幕最多500条")
        cue_ids, text_size = set(), 0
        for cue in cues:
            if (
                not isinstance(cue, dict)
                or not isinstance(cue.get("id"), str)
                or not ID.fullmatch(cue["id"])
                or cue["id"] in cue_ids
                or any(
                    type(cue.get(k)) not in (float, int) or not 0 <= cue[k] <= 360000
                    for k in ("start", "end")
                )
                or not isinstance(cue.get("text"), str)
                or len(cue["text"]) > 2000
            ):
                raise ValueError("字幕身份、时间或正文无效")
            cue_ids.add(cue["id"])
            text_size += len(cue["text"])
        if text_size > 100000:
            raise ValueError("每章字幕正文最多100000字符")
        snapshot = track.get("confirmedSnapshot")
        if snapshot is not None and (
            not isinstance(snapshot, dict)
            or not isinstance(snapshot.get("basis"), list)
            or len(snapshot["basis"]) > 5000
            or not isinstance(snapshot.get("cues"), list)
            or len(snapshot["cues"]) > 500
            or len(json.dumps(snapshot, ensure_ascii=False, allow_nan=False).encode())
            > 2 * 1024 * 1024
        ):
            raise ValueError("字幕确认依据无效或过大")
        confirmed_at = track.get("confirmedAt")
        if confirmed_at is not None and (
            not isinstance(confirmed_at, str) or len(confirmed_at) > 80
        ):
            raise ValueError("字幕确认时间无效")
    for e in entities:
        if any(
            v not in lookup or lookup[v]["type"] not in {"image", "video", "audio"}
            for v in e["data"].get("libraryFileIds", [])
        ):
            raise ValueError(
                "Filed media must belong to this story. / 归档文件必须属于当前故事。"
            )
        location_id = e["data"].get("locationId")
        if location_id and (
            location_id not in lookup or lookup[location_id]["type"] != "location"
        ):
            raise ValueError("绑定的地点不存在")
        if any(
            v not in lookup or lookup[v]["type"] != "image"
            for v in e["data"].get("referenceAssetIds", [])
        ):
            raise ValueError("地点参考图引用的图片不存在")
        for cast in e["data"].get("cast", []):
            actor_id = cast if isinstance(cast, str) else cast["characterId"]
            actor = lookup.get(actor_id)
            if not actor or actor["type"] != "character":
                raise ValueError("绑定的角色不存在")
            if (
                isinstance(cast, dict)
                and cast["lookId"]
                and not any(
                    look["id"] == cast["lookId"]
                    for look in actor["data"].get("looks", [])
                )
            ):
                raise ValueError("绑定的角色造型不存在")
        for look in e["data"].get("looks", []):
            if any(
                value and (value not in lookup or lookup[value]["type"] != "image")
                for value in look["gallery"].values()
            ):
                raise ValueError("角色图集引用的图片不存在")
        p = lookup.get(e.get("parentId"))
        if e.get("parentId") is not None and not p:
            raise ValueError("节点父级不存在")
        if (
            (e["type"] == "chapter" and p)
            or (e["type"] == "scene" and (not p or p["type"] != "chapter"))
            or (e["type"] == "shot" and (not p or p["type"] != "scene"))
            or (
                e["type"] not in {"chapter", "scene", "shot"}
                and p
                and p["type"] not in {"chapter", "scene", "shot"}
            )
        ):
            raise ValueError("章节/场景/镜头层级不正确")
        selected = e["data"].get("selectedAssetId")
        if selected not in (None, "") and (
            not isinstance(selected, str)
            or e["type"] != "shot"
            or selected not in lookup
            or lookup[selected]["type"] not in {"image", "video"}
        ):
            raise ValueError("镜头采用的候选素材无效")
        seen = {e["id"]}
        while p:
            if p["id"] in seen:
                raise ValueError("节点存在循环父级")
            seen.add(p["id"])
            p = lookup.get(p.get("parentId"))
    seen_links, seen_edges, dependencies = set(), set(), {}
    for link in links:
        if (
            not isinstance(link, dict)
            or not isinstance(link.get("id"), str)
            or not ID.fullmatch(link["id"])
            or link["id"] in seen_links
            or not isinstance(link.get("source"), str)
            or not isinstance(link.get("target"), str)
            or link.get("source") not in lookup
            or link.get("target") not in lookup
            or link.get("source") == link.get("target")
        ):
            raise ValueError("项目连线无效")
        source, target, role = (
            lookup[link["source"]],
            lookup[link["target"]],
            link.get("role"),
        )
        if (
            not isinstance(role, str)
            or role not in ROLES
            or source["type"] not in ROLES[role]
            or target["type"] not in {"shot", "generation"}
        ):
            raise ValueError("连线用途与节点类型不匹配")
        edge = (source["id"], target["id"], role)
        if edge in seen_edges:
            raise ValueError("项目连线重复")
        seen_edges.add(edge)
        if role == "dependency":
            if target["type"] != "generation":
                raise ValueError("前置步骤仅接入生成步骤")
            dependencies.setdefault(source["id"], []).append(target["id"])
        if (
            role in {"firstFrame", "lastFrame", "motion", "audio"}
            and target["type"] == "generation"
            and target["data"].get("recipe") != "video"
        ):
            raise ValueError("该连线用途仅支持视频生成步骤")
        seen_links.add(link["id"])
    # Kahn traversal avoids recursion limits on user-authored long graphs.
    degrees = {key: 0 for key in lookup}
    for targets in dependencies.values():
        for target in targets:
            degrees[target] += 1
    todo, visited = [key for key, degree in degrees.items() if not degree], 0
    while todo:
        source = todo.pop()
        visited += 1
        for target in dependencies.get(source, []):
            degrees[target] -= 1
            if not degrees[target]:
                todo.append(target)
    if visited != len(lookup):
        raise ValueError("生成步骤存在循环依赖")
    layout = project.get("layout")
    if (
        not isinstance(layout, dict)
        or not isinstance(layout.get("positions"), dict)
        or not isinstance(layout.get("viewport"), dict)
    ):
        raise ValueError("画布布局无效")
    for key, position in layout["positions"].items():
        if (
            key not in lookup
            or not isinstance(position, dict)
            or any(
                type(position.get(axis)) not in (int, float)
                or abs(position[axis]) > 1e7
                for axis in ("x", "y")
            )
        ):
            raise ValueError("画布节点位置无效")
    viewport = layout["viewport"]
    if (
        any(type(viewport.get(axis)) not in (int, float) for axis in ("x", "y", "zoom"))
        or not 0.05 <= viewport["zoom"] <= 10
    ):
        raise ValueError("画布视口无效")
    if (
        not isinstance(project.get("jobs", []), list)
        or len(project.get("jobs", [])) > 10000
    ):
        raise ValueError("项目任务记录无效")
    return project
