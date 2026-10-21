"""Pure, bounded manual-caption checks. No files, fonts, processes or network."""

from __future__ import annotations

import copy
import math
import re
import unicodedata

PRESET = "shortdrama-zh-v1"
MAX_CUES = 500
MAX_LINE_CHARACTERS = 18
_ID = re.compile(r"[A-Za-z0-9_-]{1,160}$")


def caption_signature(project, chapter_id, shots):
    """Rebuild the object saved by caption-model.js, without trusting its copy."""
    entities = {entity["id"]: entity for entity in project["entities"]}
    basis, cursor = [], 0
    for shot in shots:
        start = cursor / 24
        cursor += max(0, math.floor(shot["data"].get("seconds", 0) * 24 + 0.5))
        asset = entities.get(shot["data"].get("selectedAssetId"))
        item = {
            "id": shot["id"],
            "start": start,
            "end": cursor / 24,
            "asset": (
                {
                    "id": asset["id"],
                    "fileId": asset["data"].get("fileId") or None,
                    "missing": bool(asset["data"].get("missingFile")),
                }
                if asset
                else None
            ),
        }
        if shot["data"].get("selectedVideoRange"):
            item["selectedVideoRange"] = copy.deepcopy(
                shot["data"]["selectedVideoRange"]
            )
        basis.append(item)
    journey = project.get("journey", {})
    from .render_plans import chapter_tracks

    audio = []
    for track in chapter_tracks(project, chapter_id):
        asset_id = track.get("assetId") or track.get("audioId") or None
        asset = entities.get(asset_id)
        item = {
            "assetId": asset_id,
            "fileId": asset["data"].get("fileId") or None if asset else None,
            "shotId": track.get("shotId") or None,
            "offset": track.get("offset") if track.get("offset") is not None else 0,
            "muted": bool(track.get("muted")),
        }
        # JavaScript JSON omits an undefined property; preserve that distinction.
        item.update({key: track[key] for key in ("id", "start", "end") if key in track})
        if track.get("generatedFrom"):
            item.update(
                generatedFrom=copy.deepcopy(track["generatedFrom"]),
                needsReview=bool(track.get("needsReview")),
            )
        audio.append(item)
    cues = journey.get("captionTracks", {}).get(chapter_id, {}).get("cues", [])
    ordered = sorted(cues, key=lambda cue: (cue["start"], cue["end"]))
    return {
        "basis": basis,
        "soundMode": journey.get("sound", {}).get("mode") or None,
        "audio": audio,
        "cues": [
            {key: cue[key] for key in ("id", "start", "end", "text")} for cue in ordered
        ],
    }


def validate_caption_text(text):
    """Burn-only bounds; an unusable draft remains editable and exportable."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError("字幕正文不能为空")
    # Equivalent newline representation only; never trim or alter visible text.
    text = text.replace("\r\n", "\n")
    if any(
        char in "{}\\<>"
        or char in "\u2028\u2029"
        or (char != "\n" and unicodedata.category(char).startswith("C"))
        for char in text
    ):
        raise ValueError(
            "烧录字幕只接受纯文字：请移除大括号、反斜杠、HTML符号或控制字符；原稿仍保留"
        )
    lines = text.split("\n")
    if len(lines) > 2 or any(
        not line.strip() or len(line) > MAX_LINE_CHARACTERS for line in lines
    ):
        raise ValueError(
            "烧录字幕每条最多2行、每行1–18字符；请拆条或明确换行，不会自动删字或改行"
        )
    return text


def validate_normalized_subtitles(value, frame_count):
    """Shared planner/worker contract, with exact types and half-open frames."""
    if value is None:
        return None
    if (
        type(frame_count) is not int
        or not 1 <= frame_count <= 14400
        or not isinstance(value, dict)
        or set(value) != {"preset", "cues"}
        or value["preset"] != PRESET
        or not isinstance(value["cues"], list)
        or not 1 <= len(value["cues"]) <= MAX_CUES
    ):
        raise ValueError("烧录字幕契约、版式或数量无效")
    ids, previous_end = set(), 0
    for cue in value["cues"]:
        if (
            not isinstance(cue, dict)
            or set(cue) != {"id", "start_frame", "end_frame", "text"}
            or not isinstance(cue["id"], str)
            or not _ID.fullmatch(cue["id"])
            or cue["id"] in ids
            or type(cue["start_frame"]) is not int
            or type(cue["end_frame"]) is not int
            or not previous_end <= cue["start_frame"] < cue["end_frame"] <= frame_count
        ):
            raise ValueError("烧录字幕身份、帧范围、排序或重叠无效")
        if validate_caption_text(cue["text"]) != cue["text"]:
            raise ValueError("烧录字幕契约必须使用规范化LF换行")
        ids.add(cue["id"])
        previous_end = cue["end_frame"]
    return value


def compile_subtitles(project, chapter_id, shots, frame_count):
    track = project.get("journey", {}).get("captionTracks", {}).get(chapter_id, {})
    cues = track.get("cues") if isinstance(track, dict) else None
    value = {"preset": PRESET, "cues": []}
    blockers, warnings = [], []
    if not isinstance(cues, list) or not 1 <= len(cues) <= MAX_CUES:
        return (
            value,
            {"burned": True, **value},
            ["先为本章添加并人工确认1–500条字幕，再启用烧录"],
            [],
        )

    # The editor can append an edited cue to the raw array. Number diagnostics
    # in the same chronological order as the displayed/compiled timeline.
    # Invalid draft ranges remain reportable instead of making sorting fail.
    def cue_order(cue):
        if not isinstance(cue, dict):
            return (math.inf, math.inf)
        return tuple(
            value if type(value) in (int, float) and math.isfinite(value) else math.inf
            for value in (cue.get("start"), cue.get("end"))
        )

    ids, valid, previous_end = set(), [], 0
    for index, cue in enumerate(sorted(cues, key=cue_order), 1):
        prefix = f"第{index}条字幕："
        try:
            if (
                not isinstance(cue, dict)
                or not isinstance(cue.get("id"), str)
                or not _ID.fullmatch(cue["id"])
                or cue["id"] in ids
            ):
                raise ValueError("字幕编号无效或重复")
            ids.add(cue["id"])
            text = validate_caption_text(cue.get("text"))
            start, end = cue.get("start"), cue.get("end")
            if (
                type(start) not in (float, int)
                or type(end) not in (float, int)
                or not math.isfinite(start)
                or not math.isfinite(end)
                or not 0 <= start < end <= frame_count / 24
            ):
                raise ValueError(
                    "时间无效或超过本章成片；请修正起止时间，不会自动裁掉字幕"
                )
            first, last = math.ceil(start * 24 - 1e-7), math.floor(end * 24 + 1e-7)
            if last <= first:
                raise ValueError("向内对齐24fps后不足1帧；请延长这条字幕或调整时间")
            valid.append(
                (
                    start,
                    end,
                    {
                        "id": cue["id"],
                        "start_frame": first,
                        "end_frame": last,
                        "text": text,
                    },
                )
            )
            if abs(first / 24 - start) > 1e-7 or abs(last / 24 - end) > 1e-7:
                warnings.append(
                    prefix
                    + f"向内对齐24fps，实际显示{first / 24:.3f}–{last / 24:.3f}秒；请对照最终成片复核"
                )
        except ValueError as error:
            blockers.append(prefix + str(error))
    valid.sort(key=lambda item: (item[0], item[1]))
    for start, end, cue in valid:
        if start < previous_end - 1e-7:
            blockers.append("字幕时间有重叠，请调整原稿后重新确认；不会合并或自动移位")
        previous_end = max(previous_end, end)
        value["cues"].append(cue)
    try:
        if track.get("confirmedSnapshot") != caption_signature(
            project, chapter_id, shots
        ):
            blockers.append("这一版字幕或时间线尚未人工确认；请核对后重新确认字幕")
    except (KeyError, TypeError, ValueError):
        blockers.append("字幕确认依据无效，请重新核对并确认字幕")
    if not blockers:
        validate_normalized_subtitles(value, frame_count)
    display = {
        "burned": True,
        "preset": PRESET,
        "cues": [
            {**cue, "start": cue["start_frame"] / 24, "end": cue["end_frame"] / 24}
            for cue in value["cues"]
        ],
    }
    return value, display, list(dict.fromkeys(blockers)), warnings
