"""Validated narrative windows; receipts and model calls live in the service."""

import copy
import json
import re

from comfy_workflow import native_output_spec
from .repository import Conflict, request_hash
from .source_snapshot import source_components
from .script_window_timing import schedule

DIALOGUE = re.compile(r"\(S([1-9][0-9]*)\)\s*<d>\[([^\]\r\n]+)\]\s*([^<>]+)</d>")


def source_hash(project, shot_id):
    value = copy.deepcopy(source_components(project, shot_id))
    for entity in value["entities"]:
        entity.pop("version", None)
        entity.pop("updatedAt", None)
        entity.get("data", {}).pop("script_windows", None)
    return request_hash(value)


def source(project, shot_id, capabilities):
    from .generation_draft import read_draft, shot_entity

    shot = shot_entity(project, shot_id)
    draft, _ = read_draft(project, shot_id, capabilities=capabilities)
    prompt = draft["prompt"]
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 10000:
        raise ValueError("script_windows_invalid_source")
    if re.search(r"\[\s*/|#!PROMPT!:|!enhanced!", prompt):
        raise ValueError("script_windows_slash_commands_unsupported")
    lines = []
    for match in DIALOGUE.finditer(prompt):
        line = f"(S{match[1]}) <d>[{match[2]}] {match[3].strip()}</d>"
        lines.append({"id": f"line-{len(lines) + 1}", "text": line})
    residual = DIALOGUE.sub("", prompt)
    if re.search(r"</?d>|\(S\d+\)", residual) or len(lines) > 32:
        raise ValueError("script_windows_noncanonical_dialogue")
    duration = draft["controls"].get("duration") or shot["data"].get("seconds", 5)
    if type(duration) not in (int, float) or not 8 <= duration <= 15:
        raise ValueError("script_windows_unsupported_duration")
    if (
        draft["inputs"].get("guides")
        or draft["inputs"].get("last_frame")
        or (
            draft.get("recipe_id", "").endswith("fl2va-v1")
            and draft["inputs"].get("audios")
        )
        or draft["controls"].get("audio_conditioning", "reference")
        not in (None, "reference")
        or draft["controls"].get("video_conditioning", "reference") != "reference"
    ):
        raise ValueError("script_windows_conditioning_unsupported")
    frames = native_output_spec({"duration": duration})["frames"]
    options = {}
    for seconds in range(5, 11):
        try:
            options[seconds] = schedule(
                frames, seconds, bool(draft["inputs"].get("first_frame"))
            )
        except ValueError:
            pass
    if not options:
        raise ValueError("script_windows_no_supported_schedule")
    return {
        "source_hash": source_hash(project, shot_id),
        "prompt": prompt,
        "dialogue": lines,
        "options": options,
        "context": [
            "\n".join(
                line
                for line in DIALOGUE.sub("", part["text"]).splitlines()
                if line.strip()
            )
            for part in draft.get("storyboard", {}).get("prompt_parts", [])
            if part["kind"] != "action"
        ],
    }


def validate_result(raw, original, explicit=None):
    if not isinstance(raw, str) or len(raw) > 24000:
        raise ValueError("script_windows_invalid_model_result")

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("script_windows_duplicate_json_key")
            result[key] = value
        return result

    value = json.loads(raw, object_pairs_hook=unique)
    if not isinstance(value, dict) or set(value) != {"window_seconds", "windows"}:
        raise ValueError("script_windows_invalid_model_result")
    seconds = value["window_seconds"]
    if (
        type(seconds) is not int
        or seconds not in original["options"]
        or (explicit is not None and seconds != explicit)
    ):
        raise ValueError("script_windows_unsupported_length")
    windows = value["windows"]
    timing = original["options"][seconds]
    if not isinstance(windows, list) or len(windows) != len(timing["windows"]):
        raise ValueError("script_windows_count_mismatch")
    seen = []
    for index, window in enumerate(windows):
        if not isinstance(window, dict) or set(window) != {
            "index",
            "description",
            "dialogue_ids",
        }:
            raise ValueError("script_windows_invalid_window")
        text, ids = window["description"], window["dialogue_ids"]
        if type(window["index"]) is not int or window["index"] != index:
            raise ValueError("script_windows_invalid_index")
        if (
            not isinstance(text, str)
            or not 1 <= len(text.strip()) <= 2000
            or "\n" in text
            or "\r" in text
            or re.search(r'<|>|\[\s*/|\(S\d+\)|["“”「」]', text)
        ):
            raise ValueError("script_windows_invalid_description")
        if not isinstance(ids, list) or any(not isinstance(x, str) for x in ids):
            raise ValueError("script_windows_invalid_dialogue_ids")
        # The model assigns IDs only; it must not smuggle original speech into prose.
        if any(
            DIALOGUE.fullmatch(line["text"])[3].strip() in text
            for line in original["dialogue"]
        ):
            raise ValueError("script_windows_dialogue_in_description")
        seen.extend(ids)
    expected = [line["id"] for line in original["dialogue"]]
    if seen != expected:
        raise ValueError("script_windows_dialogue_not_exact_once")
    result = {
        "version": 1,
        "window_seconds": seconds,
        "source_hash": original["source_hash"],
        "windows": windows,
    }
    prompts = compile_prompts(result, original)
    return result, timing, prompts


def compile_prompts(value, original):
    dialogue = {line["id"]: line["text"] for line in original["dialogue"]}
    prompts = [
        "\n".join(
            [
                *original["context"],
                window["description"],
                *(dialogue[ident] for ident in window["dialogue_ids"]),
            ]
        )
        for window in value["windows"]
    ]
    if sum(map(len, prompts)) + 2 * (len(prompts) - 1) > 12000:
        raise ValueError("script_windows_prompt_too_long")
    return prompts


def apply_confirmation(project, shot_id, receipt):
    from .generation_draft import shot_entity

    shot = shot_entity(project, shot_id)
    if receipt.get("status") != "ready" or receipt.get("source_hash") != source_hash(
        project, shot_id
    ):
        raise Conflict("script_windows_stale")
    shot["data"]["script_windows"] = {
        **copy.deepcopy(receipt["script_windows"]),
        "preview_id": receipt["preview_id"],
    }
    shot["version"] += 1


def contract():
    return {
        "field": "shot.data.script_windows",
        "version": 1,
        "preview": "POST /v1/projects/{project_id}/shots/{shot_id}/script-windows/preview",
        "read": "GET /v1/projects/{project_id}/shots/{shot_id}/script-windows/previews/{preview_id}",
        "request": {
            "expected_version": "integer",
            "window_seconds": "optional integer 5..10; omitted = model chooses supported timing",
        },
        "idempotency": "Idempotency-Key required; same key never calls the model twice, including unknown outcomes.",
        "confirm": "shot.script_windows.confirm: shot_id, preview_id; normal expected_version CAS",
        "clear": "shot.script_windows.clear: shot_id; normal expected_version CAS",
        "timing": "24 fps; timing.windows contains start/end frames and seconds, generation_frames, overlap_frames. Native rounding and short final windows are visible before confirmation.",
        "limits": "3 new previews/minute/owner; one existing Gemini call; no generation or video-credit debit; 8..15 second source shots in the current native envelope.",
        "stale": "Source changes require another preview/confirm or explicit clear; no silent reuse.",
    }
