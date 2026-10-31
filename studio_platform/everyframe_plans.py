"""Cold-start backstop for H3 takes through a hosted per-render API (#918).

When no warm worker can take a representable FL/REF job, the plan is routed to
pool `everyframe` instead of waiting for a machine to boot. Credits, the tier and
the take are unchanged; only the execution differs. This module maps and checks
requests; it never contacts the provider.
"""

POOL = "everyframe"
BACKEND = "everyframe-render"
PROVIDER = "everyframe"
MODEL = "hosted-h3-max"  # Internal; users only ever see their tier.
CONFIGURATION = "everyframe-h3max-v1"
RECIPES = ("h3-base-fl2va-v1", "h3-base-ref2va-v1")
SLOTS = 4
# Vendor limit is 40 renders per 15 min per account. Past this share the take
# waits for our own machines, which then start as before.
WINDOW_SECONDS = 900
MAX_PER_WINDOW = 30
EXPECTED_RUNTIME_S = 300
VENDOR_MODEL = "h3-max-fast"
RESOLUTIONS = {"480P": "480p", "768P": "768p", "1080P": "1080p"}
ASPECTS = {"16:9", "9:16", "1:1", "4:5", "21:9"}
DURATIONS = range(5, 16)
# Vendor reference limits for h3-max-fast (GET /v1/render/models).
LIMITS = {"images": 9, "videos": 3, "audios": 3}
MAX_PROMPT = 50_000


def vendor_request(compiled):
    """The vendor render body without attachments, or None if not representable.

    Only plain requests map exactly: no time anchors, no excerpts or per-video
    audio choices, no partial frames and no mixed FL/REF inputs.
    """
    if compiled.get("recipe_id") not in RECIPES:
        return None
    request = compiled.get("request") or {}
    inputs = request.get("inputs") or {}
    resolution = RESOLUTIONS.get(request.get("resolution"))
    first, last = inputs.get("first_frame"), inputs.get("last_frame")
    refs = {kind: list(inputs.get(kind) or []) for kind in LIMITS}
    prompt, seed = request.get("prompt"), request.get("seed")
    if (
        resolution is None
        or request.get("aspect_ratio") not in ASPECTS
        or request.get("duration") not in DURATIONS
        or not isinstance(prompt, str)
        or not 1 <= len(prompt) <= MAX_PROMPT
        or request.get("guides")
        or request.get("video_excerpts")
        or request.get("audio_excerpts")
        or request.get("video_audio")
        or any(len(refs[kind]) > limit for kind, limit in LIMITS.items())
        or (last and not first)
    ):
        return None
    body = {
        "modelId": VENDOR_MODEL,
        "prompt": prompt,
        "duration": request["duration"],
        "resolution": resolution,
        "aspect": request["aspect_ratio"],
    }
    if isinstance(seed, str) and seed.isdigit() and len(seed) <= 9:
        body["seed"] = int(seed)
    if request.get("mode") == "fl":
        if any(refs.values()):
            return None
        frames = [first, last] if last else [first] if first else []
        body["referenceMode"] = (
            "first-last" if last else "first-frame" if first else "text"
        )
        return body, frames
    if request.get("mode") == "ref" and not first and any(refs.values()):
        body["referenceMode"] = "omni"
        return body, refs["images"] + refs["videos"] + refs["audios"]
    return None
