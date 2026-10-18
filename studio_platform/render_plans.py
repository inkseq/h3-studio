"""Server-derived chapter rough cuts. Browser never supplies URLs or object keys."""

from __future__ import annotations

import math

from .repository import Conflict, NotFound, request_hash
from .project_validation import validate_generated_audio_binding
from .caption_server import PRESET as CAPTION_PRESET, compile_subtitles

RECIPE = "chapter-roughcut-v1"
MODEL = "sixnine-chapter-roughcut-v1"
CONFIGURATION = "cpu-render-v3"
POOL = "cpu-render"
CONFIGURATIONS = {1: "cpu-render-v1", 2: "cpu-render-v2", 3: CONFIGURATION}
DIMENSIONS = {
    "480P": {
        "16:9": (854, 480),
        "9:16": (480, 854),
        "1:1": (480, 480),
        "4:3": (640, 480),
        "3:4": (480, 640),
    },
    "720P": {
        "16:9": (1280, 720),
        "9:16": (720, 1280),
        "1:1": (720, 720),
        "4:3": (960, 720),
        "3:4": (720, 960),
    },
}


def finite(value, minimum=0, maximum=3600):
    return (
        type(value) in (int, float)
        and math.isfinite(value)
        and minimum <= value <= maximum
    )


def frame_sample(frame):
    """Round one absolute frame boundary; never accumulate rounded frame sizes."""
    return (frame * 32000 + 12) // 24


def configuration_for(compiled):
    version = compiled.get("request", {}).get("render", {}).get("version")
    if type(version) is not int or version not in CONFIGURATIONS:
        raise ValueError("粗剪执行契约版本不受支持")
    return CONFIGURATIONS[version]


def video_range(shot, entity, source_duration, frames):
    """Bind an edit to its exact media identity; snap inward to the 24fps grid."""
    selected = shot.get("data", {}).get("selectedVideoRange")
    if selected is None:
        if source_duration + 0.001 < frames / 24:
            raise ValueError(
                f"候选只有{source_duration:.3f}秒；请缩短镜头时长或换用足够长的视频"
            )
        return 0, {
            "selected_start": 0,
            "selected_end": source_duration,
            "source_start": 0,
            "source_end": frames / 24,
            "has_selected_range": False,
        }
    if not isinstance(selected, dict) or set(selected) != {
        "assetId",
        "fileId",
        "cloudAssetId",
        "cloudArtifactId",
        "start",
        "end",
    }:
        raise ValueError("视频选段格式无效，请重新选择入点和出点")
    if selected["assetId"] != entity["id"] or any(
        selected[k] != (entity.get("data", {}).get(k) or None)
        for k in ("fileId", "cloudAssetId", "cloudArtifactId")
    ):
        raise ValueError("采用视频或原文件已变化，请重新确认入点和出点")
    start, end = selected["start"], selected["end"]
    if (
        not finite(start, 0, 360000)
        or not finite(end, 0, 360000)
        or end <= start
        or end > source_duration + 0.001
    ):
        raise ValueError("视频选段超出原片或时间无效，请重新确认")
    first, last = math.ceil(start * 24 - 1e-7), math.floor(end * 24 + 1e-7)
    if last - first < frames:
        raise ValueError(
            f"选段对齐24fps后只有{max(0, last - first) / 24:.3f}秒，短于镜头{frames / 24:.3f}秒；请调整镜头时长或选段"
        )
    return first, {
        "selected_start": start,
        "selected_end": end,
        "source_start": first / 24,
        "source_end": (first + frames) / 24,
        "aligned_range_end": last / 24,
        "has_selected_range": True,
        "unused_tail": (last - first - frames) / 24,
    }


def ordered_chapter(project, chapter_id):
    entities = project["entities"]
    chapter = next(
        (e for e in entities if e["id"] == chapter_id and e["type"] == "chapter"), None
    )
    if chapter is None:
        raise NotFound("chapter_not_found")
    # Stable project-array order matches Array.sort's stable tie handling in UI.
    scenes = sorted(
        (
            e
            for e in entities
            if e.get("parentId") == chapter_id and e["type"] == "scene"
        ),
        key=lambda e: e["order"],
    )
    shots = [
        shot
        for scene in scenes
        for shot in sorted(
            (
                e
                for e in entities
                if e.get("parentId") == scene["id"] and e["type"] == "shot"
            ),
            key=lambda e: e["order"],
        )
    ]
    return chapter, scenes, shots


def render_source_hash(project, chapter_id, *, burn_subtitles=False):
    chapter, scenes, shots = ordered_chapter(project, chapter_id)
    journey = project.get("journey", {})
    tracks = journey.get("soundTracks", {}).get(chapter_id, [])
    source_ids = {s.get("data", {}).get("selectedAssetId") for s in shots}
    if isinstance(tracks, list):
        source_ids.update(
            t.get("assetId", t.get("audioId"))
            for t in tracks
            if isinstance(t, dict)
            and isinstance(t.get("assetId", t.get("audioId")), str)
        )
    value = {
        "chapter": chapter,
        "scenes": scenes,
        "shots": shots,
        "sources": [e for e in project["entities"] if e["id"] in source_ids],
        "sound": journey.get("sound", {}),
        "tracks": tracks,
        "aspect": journey.get("brief", {}).get("aspect", "9:16"),
    }
    if burn_subtitles:
        value["captions"] = {
            "preset": CAPTION_PRESET,
            "track": journey.get("captionTracks", {}).get(chapter_id),
        }
    return request_hash(value)


def compile_render(body, project, source_resolver):
    """Resolver returns owner/project-checked immutable source and decode metadata."""
    if not isinstance(body, dict) or set(body) - {
        "client_ref",
        "resolution",
        "aspect",
        "burn_subtitles",
    }:
        raise ValueError("粗剪预检只接受章节、清晰度、画幅与字幕烧录开关")
    burn_subtitles = body.get("burn_subtitles", False)
    if type(burn_subtitles) is not bool:
        raise ValueError("字幕烧录开关必须为布尔值")
    ref = body.get("client_ref")
    if (
        not isinstance(ref, dict)
        or set(ref) != {"project_id", "chapter_id"}
        or ref["project_id"] != project["id"]
        or not isinstance(ref["chapter_id"], str)
    ):
        raise ValueError("需要明确的项目与章节")
    resolution = body.get("resolution", "720P")
    aspect = body.get(
        "aspect", project.get("journey", {}).get("brief", {}).get("aspect", "9:16")
    )
    if (
        not isinstance(resolution, str)
        or resolution not in DIMENSIONS
        or not isinstance(aspect, str)
        or aspect not in DIMENSIONS[resolution]
    ):
        raise ValueError("粗剪支持480P/720P与16:9、9:16、1:1、4:3、3:4")
    width, height = DIMENSIONS[resolution][aspect]
    chapter, scenes, shots = ordered_chapter(project, ref["chapter_id"])
    lookup = {e["id"]: e for e in project["entities"]}
    blockers, warnings, sources, render_shots, display_shots = [], [], {}, [], []
    if not 1 <= len(shots) <= 50:
        blockers.append("一章粗剪需要1–50个镜头；请先添加镜头或拆分章节")
    cursor, starts, frame_starts = 0, {}, {}

    def resolve(entity_id, kind):
        if not isinstance(entity_id, str):
            raise ValueError("尚未选择素材")
        entity = lookup.get(entity_id)
        if (
            entity is None
            or entity["type"] != kind
            or entity.get("data", {}).get("missingFile")
        ):
            raise ValueError(
                "需要可读取的" + ("视频候选" if kind == "video" else "音频素材")
            )
        if entity_id not in sources:
            source = source_resolver(entity, kind)
            if (
                source["kind"] != kind
                or not finite(source["duration"], 0.001)
                or not 0 < source["object"]["size_bytes"] <= 512 * 1024**2
            ):
                raise ValueError("素材格式、时长或大小不符合粗剪要求")
            sources[entity_id] = source
        return sources[entity_id], entity

    for shot in shots[:50]:
        seconds = shot.get("data", {}).get("seconds")
        if not finite(seconds, 1 / 24, 600):
            blockers.append(f'「{shot["title"]}」需要有效的镜头时长')
            continue
        frames = max(1, math.floor(seconds * 24 + 0.5))
        starts[shot["id"]] = (cursor / 24, frames / 24)
        frame_starts[shot["id"]] = cursor
        cursor += frames
        if abs(frames / 24 - seconds) > 0.0001:
            warnings.append(
                f'「{shot["title"]}」时长对齐24fps，实际为{frames / 24:.3f}秒'
            )
        source_id = shot["data"].get("selectedAssetId")
        try:
            source, entity = resolve(source_id, "video")
        except (ValueError, NotFound):
            blockers.append(
                f'「{shot["title"]}」候选视频不可用；请确认已上传/采用视频且长度足够'
            )
            continue
        try:
            start_frame, selected = video_range(
                shot, entity, source["duration"], frames
            )
        except ValueError as error:
            # Only locally constructed range errors enter this public response.
            blockers.append(f'「{shot["title"]}」{error}')
            continue
        render_shots.append(
            {
                "shot_id": shot["id"],
                "source_id": source_id,
                "frames": frames,
                "source_start_frame": start_frame,
            }
        )
        display_shots.append(
            {
                "shot_id": shot["id"],
                "title": shot["title"],
                "source_title": entity["title"],
                "duration": frames / 24,
                "source_duration": source["duration"],
                "simulation": source["simulation"],
                **selected,
            }
        )
        if selected["has_selected_range"]:
            if (
                abs(selected["source_start"] - selected["selected_start"]) > 0.0001
                or abs(selected["aligned_range_end"] - selected["selected_end"])
                > 0.0001
            ):
                warnings.append(
                    f'「{shot["title"]}」选段向内对齐24fps，实际入点{selected["source_start"]:.3f}秒、可用出点{selected["aligned_range_end"]:.3f}秒'
                )
            if selected["unused_tail"] > 0.0001:
                warnings.append(
                    f'「{shot["title"]}」保持镜头时长，选段尾部{selected["unused_tail"]:.3f}秒不进入成片'
                )
    if cursor > 14400:
        blockers.append("单章粗剪上限10分钟；请拆分章节")
    duration = cursor / 24
    subtitles, display_subtitles = None, None
    if burn_subtitles:
        subtitles, display_subtitles, caption_blockers, caption_warnings = (
            compile_subtitles(project, chapter["id"], shots[:50], cursor)
        )
        blockers.extend(caption_blockers)
        warnings.extend(caption_warnings)
    journey = project.get("journey", {})
    mode = journey.get("sound", {}).get("mode")
    tracks = journey.get("soundTracks", {}).get(ref["chapter_id"], [])
    audio_tracks, display_tracks, generated_shots = [], [], set()
    shots_by_id = {s["shot_id"]: s for s in render_shots}
    if not isinstance(mode, str) or mode not in {
        "silent",
        "dialogue",
        "music",
        "mixed",
    }:
        blockers.append("先选择声音方案；可选择静音先行")
    elif mode != "silent":
        if not isinstance(tracks, list) or len(tracks) > 32:
            blockers.append("单章最多32条声音轨道")
            tracks = []
        for track in tracks:
            if not isinstance(track, dict):
                blockers.append("声音轨道格式无效")
                continue
            if track.get("muted") is True:
                continue
            try:
                if track.get("needsReview") is True:
                    raise ValueError(
                        "生成声音关联已变化；请确认新声音、解除关联或静音后再粗剪"
                    )
                source_id = track.get("assetId", track.get("audioId"))
                source, entity = resolve(source_id, "audio")
                if track.get("fileId") != entity["data"].get("fileId") or not track.get(
                    "fileId"
                ):
                    raise ValueError("声音原文件已变化，请重新确认选段")
                offset, start, end, gain = (
                    track.get("offset", 0),
                    track.get("start"),
                    track.get("end"),
                    track.get("gain", 1),
                )
                binding = track.get("generatedFrom")
                if binding is not None:
                    validate_generated_audio_binding(binding)
                    shot_id = track.get("shotId")
                    shot = (
                        shots_by_id.get(shot_id) if isinstance(shot_id, str) else None
                    )
                    if shot is None or shot["source_id"] != binding["videoEntityId"]:
                        raise ValueError(
                            "生成声音对应的镜头或采用视频已变化；请重新采用声音、解除关联或静音"
                        )
                    video, video_entity = resolve(shot["source_id"], "video")
                    if (
                        video.get("source_job_id") != binding["jobId"]
                        or source.get("source_job_id") != binding["jobId"]
                        or video.get("artifact_id") != binding["videoArtifactId"]
                        or source.get("artifact_id") != binding["audioArtifactId"]
                        or video_entity["data"].get("cloudArtifactId")
                        != binding["videoArtifactId"]
                        or entity["data"].get("cloudArtifactId")
                        != binding["audioArtifactId"]
                        or source["object"].get("content_type") != "audio/flac"
                    ):
                        raise ValueError(
                            "生成声音必须是同一已完成任务、当前项目中该视频配套的独立FLAC；不能从MP4提取或混用任务"
                        )
                    if (
                        not finite(gain, 0, 1)
                        or not finite(offset)
                        or offset != 0
                        or shot_id in generated_shots
                    ):
                        raise ValueError(
                            "每个镜头只能关联一条生成声音；起点必须跟随镜头且音量为0至1"
                        )
                    first, last = (
                        frame_sample(shot["source_start_frame"]),
                        frame_sample(shot["source_start_frame"] + shot["frames"]),
                    )
                    if round(source["duration"] * 32000) < last:
                        raise ValueError(
                            "配套生成声音短于视频选段；请缩短镜头、换声音或静音，不能补静音掩盖缺失声音"
                        )
                    generated_shots.add(shot_id)
                    # start/end shown in the document are only a UI projection.
                    # Derive actual boundaries from the current video selection.
                    audio_tracks.append(
                        {
                            "source_id": source_id,
                            "timeline_start": frame_sample(frame_starts[shot_id])
                            / 32000,
                            "source_start": first / 32000,
                            "source_end": last / 32000,
                            "gain": gain,
                        }
                    )
                    display_tracks.append(
                        {
                            "title": entity["title"],
                            **audio_tracks[-1],
                            "generated_from": dict(binding),
                            "shot_id": shot_id,
                        }
                    )
                    continue
                if (
                    not finite(offset)
                    or not finite(start)
                    or not finite(end, 0.001)
                    or end <= start
                    or end > source["duration"] + 0.001
                    or not finite(gain, 0, 1)
                ):
                    raise ValueError("声音选段或音量无效，请重新确认")
                origin = (0, duration)
                if track.get("shotId"):
                    if not isinstance(track["shotId"], str):
                        raise ValueError("声音起点镜头格式无效")
                    origin = starts.get(track["shotId"])
                    if origin is None or offset >= origin[1]:
                        raise ValueError("声音起点镜头已删除或缩短，请重新选择起点")
                timeline_start = origin[0] + offset
                if timeline_start >= duration:
                    raise ValueError("声音起点超出成片长度")
                effective_end = min(end, start + duration - timeline_start)
                if effective_end < end - 0.001:
                    warnings.append(
                        f'「{entity["title"]}」超出章节的尾部将裁掉{end - effective_end:.3f}秒'
                    )
                audio_tracks.append(
                    {
                        "source_id": source_id,
                        "timeline_start": timeline_start,
                        "source_start": start,
                        "source_end": effective_end,
                        "gain": gain,
                    }
                )
                display_tracks.append(
                    {
                        "title": entity["title"],
                        "timeline_start": timeline_start,
                        "source_start": start,
                        "source_end": effective_end,
                        "gain": gain,
                    }
                )
            except (ValueError, NotFound) as error:
                blockers.append(
                    str(error) if isinstance(error, ValueError) else "声音素材不可用"
                )
        if not audio_tracks:
            blockers.append("声音方案尚无可用音轨；请添加声音或明确选择静音")
    if sum(s["object"]["size_bytes"] for s in sources.values()) > 2 * 1024**3:
        blockers.append("粗剪去重输入超过2GiB，请拆分章节")
    simulation = any(s["simulation"] for s in sources.values())
    warnings.append(
        "按镜头顺序从已确认入点截取镜头时长；未设置选段时从视频开头取片。保持比例加边，不含转场；视频原声不混入，声音仅来自明确配置的音轨"
    )
    warnings.append(
        "本次把已确认字幕永久烧进MP4，无法在播放器中关闭；SRT仍可独立导出。固定白字黑描边、左右8%/底部12%留边，并不保证避开每个平台的界面遮挡"
        if burn_subtitles
        else "本次不烧录字幕；编辑字幕不会改变这版无字幕成片"
    )
    if simulation:
        warnings.append("含模拟来源，整片会保留SIMULATION水印，不能作为H3生成效果展示")
    compiled = {
        "recipe_id": RECIPE,
        "client_ref": ref,
        "request": {
            "model": MODEL,
            "duration": duration,
            "generate_audio": bool(audio_tracks),
            "export_crf": 18,
            "render": {
                "version": 3,
                "shots": render_shots,
                "audio_tracks": audio_tracks,
                "subtitles": subtitles,
            },
        },
        "sources": {
            k: {f: v[f] for f in ("kind", "object", "simulation")}
            for k, v in sources.items()
        },
        "output_spec": {
            "width": width,
            "height": height,
            "fps": 24,
            "frame_count": cursor,
            "actual_duration": duration,
        },
        "simulation": simulation,
        "server_source_hash": render_source_hash(
            project, ref["chapter_id"], burn_subtitles=burn_subtitles
        ),
    }
    return (
        compiled,
        {
            "chapter_title": chapter["title"],
            "shots": display_shots,
            "audio_tracks": display_tracks,
            "aspect": aspect,
            "resolution": resolution,
            "duration": duration,
            "subtitles": display_subtitles,
        },
        list(dict.fromkeys(blockers)),
        warnings,
    )


def validate_render_source(project, compiled):
    try:
        render = compiled["request"]["render"]
        return compiled["server_source_hash"] == render_source_hash(
            project,
            compiled["client_ref"]["chapter_id"],
            burn_subtitles=render.get("version") == 3
            and render.get("subtitles") is not None,
        )
    except (KeyError, TypeError, NotFound):
        return False
