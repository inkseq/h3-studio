"""Bounded v5 chapter compiler; legacy normalized requests stay in render_plans."""

from .render_limits import MAX_AUDIO_TRACKS
from .caption_server import compile_subtitles
from .project_validation import validate_generated_audio_binding
from .repository import NotFound
from .render_timing import (
    crossfade,
    expanded_audio_count,
    frame_sample,
    has_speed,
    number,
    sample,
    segment_timing,
    shot_frames,
    validate_track_fields,
)
from .shot_segments import has_segments


def compile_chapter(
    body, project, resolver, chapter, shots, width, height, aspect, resolution
):
    from .render_plans import (
        MODEL,
        RECIPE,
        chapter_tracks,
        render_source_hash,
        video_range,
    )

    ref = body["client_ref"]
    burn = body.get("burn_subtitles", False)
    lookup = {entity["id"]: entity for entity in project["entities"]}
    sources, normalized, displayed, starts = {}, [], [], {}
    shot_clips, clip_starts = {}, []
    blockers, warnings, cursor = [], [], 0
    boundary = crossfade(project, chapter["id"])
    if not 1 <= len(shots) <= 50:
        blockers.append("一章粗剪需要1–50个镜头")
    if any(has_segments(shot) for shot in shots):
        try:
            if expanded_audio_count(project, chapter["id"], shots) > MAX_AUDIO_TRACKS:
                blockers.append(f"单章展开后最多{MAX_AUDIO_TRACKS}条声音轨道")
        except ValueError as error:
            blockers.append(str(error))

    def resolve(ident, kind):
        entity = lookup.get(ident) if isinstance(ident, str) else None
        if (
            entity is None
            or entity["type"] != kind
            or entity.get("data", {}).get("missingFile")
        ):
            raise ValueError("所选视频或声音素材不可用")
        if ident not in sources:
            value = resolver(entity, kind)
            if (
                value["kind"] != kind
                or not number(value["duration"], 0.001, 3600)
                or not 0 < value["object"]["size_bytes"] <= 512 * 1024**2
            ):
                raise ValueError("素材格式、时长或大小不符合粗剪要求")
            sources[ident] = value
        return sources[ident], entity

    for shot in shots[:50]:
        try:
            ident = shot.get("data", {}).get("selectedAssetId")
            source, entity = resolve(ident, "video")
            if has_segments(shot):
                clips = [
                    (
                        clip["start_frame"],
                        clip["frames"],
                        clip["output_frames"],
                        clip["playback_rate"],
                        {
                            "selected_start": clip["start"],
                            "selected_end": clip["end"],
                            "source_start": clip["start"],
                            "source_end": clip["end"],
                            "aligned_range_end": clip["end"],
                            "has_selected_range": True,
                            "unused_tail": 0,
                        },
                    )
                    for clip in segment_timing(shot, entity, source["duration"])
                ]
            else:
                clips = None
                source_frames, frames, rate = shot_frames(shot)
            selected = shot.get("data", {}).get("selectedVideoRange")
            if clips is None and has_speed(shot):
                if set(selected) != {
                    "assetId",
                    "fileId",
                    "cloudAssetId",
                    "cloudArtifactId",
                    "start",
                    "end",
                    "playback_rate",
                }:
                    raise ValueError("视频选段格式无效")
                legacy = {
                    **shot,
                    "data": {
                        **shot["data"],
                        "selectedVideoRange": {
                            key: value
                            for key, value in selected.items()
                            if key != "playback_rate"
                        },
                    },
                }
            else:
                legacy = shot
            if clips is None:
                first, details = video_range(
                    legacy, entity, source["duration"], source_frames
                )
                clips = [(first, source_frames, frames, rate, details)]
            if len(normalized) + len(clips) > 50:
                raise ValueError("单章粗剪展开后最多50个片段")
            if any(first + length > 86400 for first, length, *_ in clips):
                raise ValueError("视频源范围超限")
            starts[shot["id"]] = (cursor, sum(clip[2] for clip in clips))
            shot_clips[shot["id"]] = []
            for first, source_frames, frames, rate, details in clips:
                shot_clips[shot["id"]].append(len(normalized))
                clip_starts.append(cursor)
                normalized.append(
                    {
                        "shot_id": shot["id"],
                        "source_id": ident,
                        "source_start_frame": first,
                        "source_frames": source_frames,
                        "frames": frames,
                        "playback_rate": rate,
                    }
                )
                displayed.append(
                    {
                        "shot_id": shot["id"],
                        "title": shot["title"],
                        "source_title": entity["title"],
                        **details,
                        "duration": frames / 24,
                        "source_duration": source["duration"],
                        "simulation": source["simulation"],
                        "source_frames": source_frames,
                        "output_frames": frames,
                        "playback_rate": rate,
                    }
                )
                cursor += frames
        except (ValueError, NotFound) as error:
            blockers.append(
                str(error) if isinstance(error, ValueError) else "视频素材不可用"
            )
    if cursor > 14400:
        blockers.append("单章粗剪上限10分钟")
    duration = cursor / 24
    subtitles, display_subtitles = None, None
    if burn:
        subtitles, display_subtitles, errors, notices = compile_subtitles(
            project, chapter["id"], shots[:50], cursor, render_version=5
        )
        blockers.extend(errors)
        warnings.extend(notices)
    journey = project.get("journey", {})
    mode = journey.get("sound", {}).get("mode")
    raw_tracks = chapter_tracks(project, chapter["id"])
    audio, display_audio, coupled, muted_tracks = [], [], {}, 0
    coupled_shots = set()
    shot_lookup = {
        shot_id: normalized[indices[0]] for shot_id, indices in shot_clips.items()
    }
    if mode not in {"silent", "dialogue", "music", "mixed"}:
        blockers.append("先选择声音方案")
    elif mode != "silent":
        if not isinstance(raw_tracks, list) or len(raw_tracks) > MAX_AUDIO_TRACKS:
            blockers.append(f"单章最多{MAX_AUDIO_TRACKS}条声音轨道")
            raw_tracks = []
        for track in raw_tracks:
            try:
                if not isinstance(track, dict):
                    raise ValueError("声音轨道格式无效")
                validate_track_fields(track)
                if track.get("muted") is True:
                    muted_tracks += 1
                    continue
                if track.get("needsReview") is True:
                    raise ValueError("生成声音关联已变化；请确认、分离或静音")
                binding, detached = (
                    track.get("generatedFrom"),
                    track.get("detached", False),
                )
                shot_id = track.get("shotId")
                shot = shot_lookup.get(shot_id) if isinstance(shot_id, str) else None
                if binding is not None:
                    validate_generated_audio_binding(binding)
                    if not detached:
                        if (
                            shot is None
                            or shot["source_id"] != binding["videoEntityId"]
                        ):
                            raise ValueError("生成声音对应的镜头或采用视频已变化")
                        if shot["playback_rate"] < 0.75 and not track.get(
                            "slow_rate_audio_enabled", False
                        ):
                            display_audio.append(
                                {
                                    "shot_id": shot_id,
                                    "effective_muted": True,
                                    "reason": "slow_rate_default",
                                }
                            )
                            muted_tracks += 1
                            continue
                    elif shot_id != "":
                        raise ValueError("分离声音须保留独立绝对起点")
                ident = track.get("assetId", track.get("audioId"))
                source, entity = resolve(ident, "audio")
                if not track.get("fileId") or track["fileId"] != entity["data"].get(
                    "fileId"
                ):
                    raise ValueError("声音原文件已变化")
                gain, offset = track.get("gain", 1), track.get("offset", 0)
                if not number(gain, 0, 1) or not number(offset, 0, 3600):
                    raise ValueError("声音音量或起点无效")
                if binding is not None:
                    # Detached sound proves its ORIGINAL owned same-job pair,
                    # without current-selection authority or unused video input.
                    if detached:
                        video = resolver(
                            {
                                "id": binding["videoEntityId"],
                                "type": "video",
                                "data": {"cloudArtifactId": binding["videoArtifactId"]},
                            },
                            "video",
                        )
                    else:
                        video = sources[shot["source_id"]]
                    if (
                        video.get("source_job_id") != binding["jobId"]
                        or source.get("source_job_id") != binding["jobId"]
                        or video.get("artifact_id") != binding["videoArtifactId"]
                        or source.get("artifact_id") != binding["audioArtifactId"]
                        or entity["data"].get("cloudArtifactId")
                        != binding["audioArtifactId"]
                        or source["object"].get("content_type") != "audio/flac"
                    ):
                        raise ValueError(
                            "生成声音必须为当前拥有的同一已完成任务配套独立FLAC"
                        )
                if binding is not None and not detached:
                    if offset != 0 or shot_id in coupled_shots:
                        raise ValueError("每个镜头只可关联一条原声且起点须跟随镜头")
                    spans = []
                    for index in shot_clips[shot_id]:
                        clip = normalized[index]
                        beginning, frames = clip_starts[index], clip["frames"]
                        timeline = frame_sample(beginning)
                        spans.append(
                            (
                                frame_sample(clip["source_start_frame"]),
                                frame_sample(
                                    clip["source_start_frame"] + clip["source_frames"]
                                ),
                                timeline,
                                frame_sample(beginning + frames) - timeline,
                                clip["playback_rate"],
                                index,
                            )
                        )
                else:
                    start, end = track.get("start"), track.get("end")
                    if (
                        not number(start, 0, 3600)
                        or not number(end, 0.001, 3600)
                        or end <= start
                        or end > source["duration"] + 0.001
                    ):
                        raise ValueError("声音选段无效")
                    first, last = sample(start), sample(end)
                    if first >= last:
                        raise ValueError("声音选段不足一个采样")
                    origin = 0
                    if not detached and shot_id:
                        if shot_id not in starts or offset >= starts[shot_id][1] / 24:
                            raise ValueError("声音起点镜头已删除或缩短")
                        origin = starts[shot_id][0] / 24
                    timeline = sample(origin + offset)
                    output, rate = last - first, 1
                    if timeline + output > frame_sample(cursor):
                        if detached:
                            raise ValueError("分离声音尾部超出章节；请明确修剪")
                        last -= timeline + output - frame_sample(cursor)
                        output = last - first
                        warnings.append("超出章节的手动声音尾部将裁掉")
                    spans = [(first, last, timeline, output, rate, None)]
                fade_in, fade_out = (
                    sample(track.get("fade_in_s", 0)),
                    sample(track.get("fade_out_s", 0)),
                )
                # Validate the entire expansion before accepting any of this track.
                if len(audio) + len(spans) > MAX_AUDIO_TRACKS:
                    raise ValueError(f"单章展开后最多{MAX_AUDIO_TRACKS}条声音轨道")
                for first, last, timeline, output, rate, index in spans:
                    if (
                        timeline < 0
                        or timeline >= frame_sample(cursor)
                        or output <= 0
                        or last > sample(source["duration"])
                    ):
                        raise ValueError("声音源或时间线范围不足；不能补静音掩盖缺失")
                    if fade_in + fade_out > output:
                        raise ValueError("声音淡入淡出超出片段或相互重叠")
                for first, last, timeline, output, rate, index in spans:
                    value = {
                        "source_id": ident,
                        "timeline_start_sample": timeline,
                        "source_start_sample": first,
                        "source_end_sample": last,
                        "output_samples": output,
                        "playback_rate": rate,
                        "gain": gain,
                        "fade_in_samples": fade_in,
                        "fade_out_samples": fade_out,
                    }
                    audio.append(value)
                    display_audio.append(
                        {
                            **value,
                            "title": entity["title"],
                            "detached": detached,
                            "effective_muted": False,
                        }
                    )
                    if index is not None:
                        coupled[index] = value
                if binding is not None and not detached:
                    coupled_shots.add(shot_id)
            except (ValueError, NotFound) as error:
                blockers.append(
                    str(error) if isinstance(error, ValueError) else "声音素材不可用"
                )
        for index in range(len(normalized) - 1):
            outgoing, incoming = (
                coupled.get(index),
                coupled.get(index + 1),
            )
            if (
                outgoing is None
                or incoming is None
                or not outgoing["gain"]
                or not incoming["gain"]
            ):
                continue
            for value, edge in (
                (outgoing, "fade_out_samples"),
                (incoming, "fade_in_samples"),
            ):
                value[edge] = max(
                    value[edge], min(sample(boundary / 2), value["output_samples"] // 2)
                )
        for value in audio:
            if (
                value["fade_in_samples"] + value["fade_out_samples"]
                > value["output_samples"]
            ):
                blockers.append("原声边界淡化与手动淡化相互重叠")
        for display in display_audio:
            if not display.get("effective_muted"):
                value = next(
                    item
                    for item in audio
                    if item["source_id"] == display["source_id"]
                    and item["timeline_start_sample"]
                    == display["timeline_start_sample"]
                )
                display.update(
                    fade_in_samples=value["fade_in_samples"],
                    fade_out_samples=value["fade_out_samples"],
                )
        if not audio and not muted_tracks:
            blockers.append("声音方案尚无可用音轨；可明确选择静音")
    # Failed/muted metadata checks never add an unused byte dependency.
    needed = {item["source_id"] for item in [*normalized, *audio]}
    sources = {key: value for key, value in sources.items() if key in needed}
    if sum(value["object"]["size_bytes"] for value in sources.values()) > 2 * 1024**3:
        blockers.append("粗剪输入超过2GiB")
    simulation = any(value["simulation"] for value in sources.values())
    compiled = {
        "recipe_id": RECIPE,
        "client_ref": ref,
        "request": {
            "model": MODEL,
            "duration": duration,
            "generate_audio": bool(audio),
            "export_crf": 18,
            "render": {
                "version": 5,
                "stage": "chapter",
                "shots": normalized,
                "audio_tracks": audio,
                "subtitles": subtitles,
            },
        },
        "sources": {
            key: {field: value[field] for field in ("kind", "object", "simulation")}
            for key, value in sources.items()
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
            project, chapter["id"], burn_subtitles=burn
        ),
    }
    return (
        compiled,
        {
            "chapter_title": chapter["title"],
            "shots": displayed,
            "audio_tracks": display_audio,
            "aspect": aspect,
            "resolution": resolution,
            "duration": duration,
            "subtitles": display_subtitles,
            "clip_sound_crossfade_s": boundary,
        },
        list(dict.fromkeys(blockers)),
        warnings,
    )
