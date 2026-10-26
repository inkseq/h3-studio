"""Advisory generation lint. Pure functions: no edits, model call or admission."""

import copy
import hashlib
import json
import math
import re

CODES = ("simultaneous_speakers", "speech_too_dense", "action_density", "plot_density")
DIALOGUE = re.compile(r"<d>\s*(?:\[[^]\r\n]+\]\s*)?(.*?)</d>", re.I | re.S)
OVERLAP = re.compile(
    r"\b(simultaneously|in unison|at the same time|talk over each other)\b|同时(?:说|喊|讲话|开口)|一起(?:说|喊)|异口同声",
    re.I,
)
BEATS = re.compile(
    r"\b(?:then|next|afterwards|suddenly)\b|然后|随后|接着|紧接着|突然", re.I
)
SCENES = re.compile(
    r"\b(?:cut to|meanwhile|days? later|years? later|flashback)\b|切换到|切到|与此同时|第二天|多年后|回忆起",
    re.I,
)
TEXT = {
    "simultaneous_speakers": (
        "Several people speak at once.",
        "多个人物同时说话。",
        "Give each speaker a separate shot or window; second-speaker lip sync is a known model limit.",
        "把不同人物的台词分到独立镜头或窗口；第二说话人口型属于已知模型限制。",
    ),
    "speech_too_dense": (
        "Speech may not fit the selected duration.",
        "台词可能超出当前时长。",
        "Split the lines across shots/windows without repeating a line; review timing before generation.",
        "把台词分到多个镜头或窗口，每句只说一次，生成前确认时长。",
    ),
    "action_density": (
        "Many successive actions are packed into one shot.",
        "一个镜头内安排了较多连续动作。",
        "Keep one main action per shot and propose additional shots for the rest.",
        "每个镜头保留一个主要动作，其余动作另提拆分建议。",
    ),
    "plot_density": (
        "Several scene or time changes occur in one short shot.",
        "短镜头内包含场景或时间变化。",
        "Separate changes of scene/time into shots and ask the user to confirm the split.",
        "把场景或时间变化分成独立镜头，确认拆分后再修改。",
    ),
}


def contract():
    return {
        "location": "Existing story-chat turn.messages[].proposal.risk_review and turn.proposal.risk_review; no new endpoint/call.",
        "codes": list(CODES),
        "semantics": "Advisory rule + model report on the proposed shots. Never an edit, automatic split, admission gate or redo. Rule heuristics are not model capability limits or a guarantee.",
        "statuses": "status=warnings/no_findings/incomplete/stale; rule_status=checked/partial; model_status=reported/not_checked/invalid/stale. no_findings is no detected issue, not a safety guarantee.",
        "recovery": "Original turn replay preserves its model receipt without another call. Changed shot source marks the presented report stale. Refresh via an explicit new conversation turn, never an automatic paid retry.",
        "model": "Optional bounded risk_review list in the existing assistant response; missing/invalid assessment stays explicit. Generation confirmation, 5-step settings and canonical dialogue tags are unchanged.",
    }


def source_hash(shot_ids, lookup):
    sources = []
    for ident in shot_ids:
        shot = lookup.get(ident)
        if not shot or shot.get("type") != "shot":
            return None
        data = shot.get("data", {})
        sources.append(
            {
                "id": ident,
                "text": data.get("prompt", shot.get("description", "")),
                "seconds": data.get("seconds", 5),
                "cast": data.get("cast", []),
                "place": (data.get("storyboard") or {}).get("place"),
                "props": (data.get("storyboard") or {}).get("propIds", []),
            }
        )
    return hashlib.sha256(
        json.dumps(
            sources, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()


def _warning(ident, code, language, *, source="rule", suggestion=None):
    zh = language == "zh"
    text = TEXT[code]
    return {
        "shot_id": ident,
        "code": code,
        "source": source,
        "message": text[1 if zh else 0],
        "suggestion": suggestion or text[3 if zh else 2],
    }


def review(shot_ids, lookup, refs, plan, *, language="en", model_fresh=True):
    warnings, partial = [], False
    for ident in shot_ids:
        shot = lookup[ident]
        data = shot.get("data", {})
        text = data.get("prompt", shot.get("description", ""))
        seconds = data.get("seconds", 5)
        if (
            not isinstance(text, str)
            or type(seconds) not in (int, float)
            or not math.isfinite(seconds)
            or seconds <= 0
        ):
            partial = True
            continue
        # Do not treat the content of a spoken line as stage directions.
        visual = DIALOGUE.sub("", text)
        speakers = set(re.findall(r"\(S(\d+)\)\s*<d>", text, re.I))
        if OVERLAP.search(visual) and (
            len(speakers) > 1
            or re.search(
                r"\b(both|two|three|everyone|they)\b|两人|三人|大家|他们|她们",
                visual,
                re.I,
            )
        ):
            warnings.append(_warning(ident, "simultaneous_speakers", language))
        speech = " ".join(DIALOGUE.findall(text))
        han = len(re.findall(r"[\u3400-\u9fff]", speech))
        words = len(re.findall(r"[A-Za-z0-9]+(?:['’-][A-Za-z0-9]+)*", speech))
        # Conservative advisory estimates, not a speech synthesizer or hard cap.
        if han / 6 + words / 3.5 > seconds:
            warnings.append(_warning(ident, "speech_too_dense", language))
        if len(BEATS.findall(visual)) + 1 > max(2, math.ceil(seconds / 2)):
            warnings.append(_warning(ident, "action_density", language))
        scene_changes = len(SCENES.findall(visual))
        if scene_changes >= 2 or scene_changes and seconds <= 5:
            warnings.append(_warning(ident, "plot_density", language))

    model_status = "not_checked"
    model_warnings = []
    if not model_fresh:
        model_status = "stale"
    elif "risk_review" in plan:
        raw = plan["risk_review"]
        valid = isinstance(raw, list) and len(raw) <= 60
        if valid:
            for item in raw:
                if not isinstance(item, dict) or set(item) != {
                    "shot",
                    "codes",
                    "suggestion",
                }:
                    valid = False
                    break
                ref, codes, suggestion = item["shot"], item["codes"], item["suggestion"]
                ident = refs.get(ref, ref) if isinstance(ref, str) else None
                if (
                    ident not in shot_ids
                    or not isinstance(codes, list)
                    or not 1 <= len(codes) <= len(CODES)
                    or any(
                        not isinstance(code, str) or code not in CODES for code in codes
                    )
                    or len(set(codes)) != len(codes)
                    or not isinstance(suggestion, str)
                    or not 1 <= len(suggestion.strip()) <= 500
                ):
                    valid = False
                    break
                model_warnings.extend(
                    _warning(
                        ident,
                        code,
                        language,
                        source="model",
                        suggestion=suggestion.strip(),
                    )
                    for code in codes
                )
        model_status = "reported" if valid else "invalid"
        if valid:
            warnings.extend(model_warnings)
    # Bounded independently of model output; one finding per shot/code/source.
    warnings = list(
        {(w["shot_id"], w["code"], w["source"]): w for w in warnings}.values()
    )
    return {
        "version": 1,
        "source_hash": source_hash(shot_ids, lookup),
        "status": "warnings"
        if warnings
        else (
            "no_findings"
            if not partial and model_status == "reported"
            else "incomplete"
        ),
        "rule_status": "partial" if partial else "checked",
        "model_status": model_status,
        "warnings": warnings,
    }


def presented_proposal(proposal, project, *, cache=None):
    """Mark old advice stale on reads, without rewriting its stored receipt."""
    value = copy.deepcopy(proposal)
    if not isinstance(value, dict) or not isinstance(value.get("risk_review"), dict):
        return value
    report = value["risk_review"]
    ids = value.get("shot_ids", [])
    if (
        not isinstance(ids, list)
        or len(ids) > 60
        or any(not isinstance(i, str) for i in ids)
    ):
        report["status"] = "stale"
        return value
    lookup = {e["id"]: e for e in project["entities"]}
    cache = cache if cache is not None else {}
    key = tuple(ids)
    if key not in cache:
        cache[key] = source_hash(ids, lookup)
    if report.get("source_hash") != cache[key]:
        report["status"] = "stale"
    return value
