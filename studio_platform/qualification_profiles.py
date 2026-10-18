"""Explicit production qualification contracts; these constants grant no capacity.

The queued-task profile admits runtime-checked workers without a synthetic
inference suite. Their first real queued job supplies request-specific proof.
Legacy profiles still run their selected suite before worker registration.
Historical evidence and this operator opt-in are never current inference proof.
The optional suite runs FL at 50 steps and the new input paths at 4 steps. Its
2048-pixel fixtures verify execution compatibility, not Ref50 speed or quality.
"""

FL_RECIPE = "h3-base-fl2va-v1"
REF_RECIPE = "h3-base-ref2va-v1"
FL50_PROFILE = "fl50"
MULTIMODAL_PROFILE = "fl50-firstlast4-ref4-v1"
QUEUED_TASK_PROFILE = "queued-task-first-v1"
RUNTIME_PROFILES = {MULTIMODAL_PROFILE, QUEUED_TASK_PROFILE}
PROFILE_RECIPES = {
    FL50_PROFILE: (FL_RECIPE,),
    MULTIMODAL_PROFILE: (FL_RECIPE, REF_RECIPE),
    QUEUED_TASK_PROFILE: (FL_RECIPE, REF_RECIPE),
}

MULTIMODAL_INPUT_LIMITS = {
    "max_images": 1,
    "max_videos": 1,
    "max_audios": 1,
    "max_image_pixels": 2048 * 2048,
    "max_video_pixels": 832 * 480,
    "max_video_duration_seconds": 107 / 24,
    "max_audio_duration_seconds": 4.45,
    "guide_kinds": ["image"],
    "guide_recipe_ids": [REF_RECIPE],
    "max_guide_time_seconds": 5,
    "allow_video_audio": True,
}

# Conservative submission allowances, not speed promises. Actual GPU rental
# remains bounded by the existing instance reservation and provider TTL.
STAGE_RUNTIME_S = {"fl50": 1200, "firstlast4": 600, "ref4": 1200}
MIN_MULTIMODAL_JOB_RUNTIME_S = 1800
