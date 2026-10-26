"""Creator input policies; never applied to accepted job receipts."""

import copy

TAOMATE = "h3-pruned-rank8-int8-sdpa-p4-29gib-taomate5-v7"
POLICY = "taomate-creator-v1"
CONTROLS_V6 = "h3-pruned-rank8-int8-sdpa-p4-29gib-controls-v6"
V6_POLICY = "controls-v6-creator-v1"
# resolution is the existing wire representation of Standard / HD (#582).
# Mode, quality selection, copies and reference assets live outside controls.
CREATOR_CONTROLS = frozenset(
    {"duration", "seed", "resolution", "aspect_ratio", "generate_audio"}
)


def policy_for_profile(profile_id):
    if profile_id == TAOMATE:
        return POLICY
    if profile_id == CONTROLS_V6:
        return V6_POLICY
    return None


def controls(profile_id, value):
    if policy_for_profile(profile_id) is None or not isinstance(value, dict):
        return copy.deepcopy(value)
    return {
        key: copy.deepcopy(item)
        for key, item in value.items()
        if key in CREATOR_CONTROLS
    }


def effective_creator_project(project, shot_id, defaults, original):
    """Read-only recipe freshness comparison uses the same creator policy."""
    from .storyboard_versions import effective_project
    from .generation_draft import shot_entity

    rendered, provenance = effective_project(project, shot_id, defaults)
    # Historical receipts keep their original comparison.
    # Never reinterpret an accepted take as if it used today's creator policy.
    policy = original.get("creator_control_policy")
    if policy not in (POLICY, V6_POLICY):
        return rendered, provenance
    h3 = shot_entity(rendered, shot_id)["data"].get("h3", {})
    if "controls" in h3:
        profile = h3.get("deploymentProfileId") or original.get("deployment_profile_id")
        if policy_for_profile(profile) == policy:
            h3["controls"] = controls(profile, h3["controls"])
    return rendered, provenance
