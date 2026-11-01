"""Hosted video models and their request mapping (#918, #931 step 5a).

One row per hosted model. A row says what the model accepts, how its prompt is
adapted, how long a render may take before the job moves on, and its list
price, which only orders a chain; the cost recorded is the vendor's own. The
owner's decisions of 2026-11-01 set the rows: models that refused faces or IP
characters in the #929 moderation round are not here at all, and chains are
ordered by price within what a shot needs. Limits, resolutions and plain
prices come from the vendor's GET /v1/render/models (2026-11-01); omni prices
are measured (#929), since the vendor table covers plain renders only. The
real charge comes from the free POST /render/quote and the render itself, but
quote does not validate the mode, so modes are gated here. The vendor has no
cancel: a submitted render runs to completion and settles. This module never
contacts a vendor.
"""

from dataclasses import dataclass

PROVIDER = "everyframe"
BACKEND = "everyframe-render"
POOL = "everyframe"
RECIPES = ("h3-base-fl2va-v1", "h3-base-ref2va-v1")
SLOTS_PER_MODEL = 4
# Turbo and fast insert their own camera cuts and follow lighting words
# literally (#918, 2026-11-01); our shots are already split by the storyboard.
NO_CUT = "Single continuous shot with no cuts, no scene changes and steady lighting."
RESOLUTIONS = {"480P": "480p", "768P": "768p", "1080P": "1080p", "2K": "2K"}


@dataclass(frozen=True)
class HostedModel:
    id: str
    configuration_id: str
    modes: frozenset  # vendor referenceMode values it serves
    resolutions: frozenset  # our resolution names
    aspects: frozenset
    max_images: int
    max_videos: int
    max_audios: int
    no_cut: bool
    # USD per 5 s by (resolution, plain|references); orders chains only.
    price_5s: dict
    # A move pays for a second render, so this outlasts the vendor's measured
    # batch queue (about 250-290 s, #929). Vivix waits for its shared queue.
    deadline_s: int
    max_prompt: int = 50_000
    # Vivix ignores the requested size and always renders 1264x720.
    vendor_resolution: str | None = None
    concurrency: int | None = None  # None: no limit known


H3 = frozenset({"480P", "768P", "1080P"})
WIDE = frozenset({"16:9", "9:16", "1:1", "4:5", "21:9"})
PLAIN = frozenset({"text", "first-frame", "first-last"})

MODELS = {
    model.id: model
    for model in (
        HostedModel(
            "h3-max-turbo",
            "everyframe-h3-max-turbo-v1",
            PLAIN,  # Refuses reference people and voice (400 bad_attachment).
            H3,
            WIDE,
            0,
            0,
            0,
            True,
            {
                ("480P", "plain"): 0.038,
                ("768P", "plain"): 0.06,
                ("1080P", "plain"): 0.12,
            },
            300,
        ),
        HostedModel(
            "pika-h3-flex",
            "everyframe-pika-h3-flex-v1",
            PLAIN | {"omni"},
            H3 | {"2K"},
            WIDE,
            9,
            3,
            3,
            False,  # Pika does not cut on its own.
            {
                ("480P", "plain"): 0.058,
                ("768P", "plain"): 0.118,
                ("1080P", "plain"): 0.206,
                ("2K", "plain"): 0.236,
                ("480P", "references"): 0.176,
                ("768P", "references"): 0.354,
                ("1080P", "references"): 0.558,
            },
            300,
            max_prompt=8000,
        ),
        HostedModel(
            "h3-max-fast",
            "everyframe-h3-max-fast-v1",
            PLAIN | {"omni"},
            H3,
            WIDE,
            9,
            3,
            3,
            True,
            {
                ("480P", "plain"): 0.076,
                ("768P", "plain"): 0.12,
                ("1080P", "plain"): 0.24,
                ("480P", "references"): 0.15,
                ("768P", "references"): 0.24,
                ("1080P", "references"): 0.48,
            },
            300,
        ),
        HostedModel(
            # Economy only: one render at a time across all vendor customers,
            # always 1264x720 with sound; scaled about 6.7% up to 768P.
            "vivix-w1",
            "everyframe-vivix-w1-v1",
            PLAIN | {"omni"},
            frozenset({"768P"}),  # The economy tier is always 768P (owner 11-02).
            # 4:5 comes back as 3:4, which crops cleanly; 21:9 comes back as 16:9
            # and would lose a quarter of its height, so it goes to turbo.
            frozenset({"16:9", "9:16", "1:1", "4:5"}),
            5,
            0,
            3,
            False,
            {("768P", "plain"): 0.01, ("768P", "references"): 0.01},
            1800,
            vendor_resolution="720p",
            concurrency=1,
        ),
    )
}
CONFIGURATIONS = {model.configuration_id: model.id for model in MODELS.values()}
# Ordering per need (owner, 2026-11-01); chain() also filters by capability.
CHAINS = {
    "plain": ("h3-max-turbo", "pika-h3-flex", "h3-max-fast"),
    "references": ("h3-max-fast", "pika-h3-flex"),
    "2k": ("pika-h3-flex",),
    "economy": ("vivix-w1", "h3-max-turbo"),
}


def vendor_request(compiled, model_id):
    """(vendor body without attachments, attachment ids in order), or None.

    None means this model cannot represent the take exactly: the job then
    tries the next model in its chain, or stays on our own machines. Seeds
    are not sent; no hosted model honours them (#929).
    """
    model = MODELS.get(model_id)
    if model is None or compiled.get("recipe_id") not in RECIPES:
        return None
    request = compiled.get("request") or {}
    inputs = request.get("inputs") or {}
    first, last = inputs.get("first_frame"), inputs.get("last_frame")
    refs = {
        "images": list(inputs.get("images") or []),
        "videos": list(inputs.get("videos") or []),
        "audios": list(inputs.get("audios") or []),
    }
    prompt = request.get("prompt")
    if (
        request.get("resolution") not in model.resolutions
        or request.get("aspect_ratio") not in model.aspects
        or request.get("duration") not in range(5, 16)
        or not isinstance(prompt, str)
        or not 1 <= len(prompt) <= model.max_prompt - len(NO_CUT) - 1
        or request.get("guides")
        or request.get("video_excerpts")
        or request.get("audio_excerpts")
        or request.get("video_audio")
        or len(refs["images"]) > model.max_images
        or len(refs["videos"]) > model.max_videos
        or len(refs["audios"]) > model.max_audios
        or (last and not first)
    ):
        return None
    if model.no_cut and NO_CUT not in prompt:
        prompt = prompt.rstrip() + " " + NO_CUT
    body = {
        "modelId": model.id,
        "prompt": prompt,
        "duration": request["duration"],
        "resolution": model.vendor_resolution or RESOLUTIONS[request["resolution"]],
        "aspect": request["aspect_ratio"],
    }
    if request.get("mode") == "fl" and not any(refs.values()):
        mode = "first-last" if last else "first-frame" if first else "text"
        attachments = [first, last] if last else [first] if first else []
    elif request.get("mode") == "ref" and not first and any(refs.values()):
        mode = "omni"
        attachments = refs["images"] + refs["videos"] + refs["audios"]
    else:
        return None
    if mode not in model.modes:
        return None
    body["referenceMode"] = mode
    return body, attachments


def need(compiled):
    """The chain a take needs: economy, plain, references or 2k."""
    if (compiled or {}).get("priority_class") == "economy":
        return "economy"
    request = (compiled or {}).get("request") or {}
    if request.get("resolution") == "2K":
        return "2k"
    return "references" if request.get("mode") == "ref" else "plain"


def price(model_id, compiled):
    """List USD for this take on this model, or None if not priced."""
    request = (compiled or {}).get("request") or {}
    kind = "references" if request.get("mode") == "ref" else "plain"
    per_5s = MODELS[model_id].price_5s.get((request.get("resolution"), kind))
    duration = request.get("duration")
    if per_5s is None or type(duration) is not int:
        return None
    return per_5s * duration / 5


def chain(compiled, name=None):
    """Models that can render this take exactly, cheapest first."""
    candidates = CHAINS[name or need(compiled)]
    usable = [
        model_id
        for model_id in candidates
        if vendor_request(compiled, model_id) is not None
        and price(model_id, compiled) is not None
    ]
    return sorted(usable, key=lambda model_id: price(model_id, compiled))
