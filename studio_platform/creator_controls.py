"""Creator-only TaoMate input policy; never applied to accepted job receipts."""

import copy

TAOMATE = "h3-pruned-rank8-int8-sdpa-p4-29gib-taomate5-v7"
POLICY = "taomate-creator-v1"
# resolution is the existing wire representation of Standard / HD (#582).
# Mode, quality selection, copies and reference assets live outside controls.
CREATOR_CONTROLS = frozenset({"duration", "seed", "resolution"})


def controls(profile_id, value):
    if profile_id != TAOMATE or not isinstance(value, dict):
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
    if original.get("creator_control_policy") != POLICY:
        return rendered, provenance
    h3 = shot_entity(rendered, shot_id)["data"].get("h3", {})
    if "controls" in h3:
        profile = h3.get("deploymentProfileId") or original.get("deployment_profile_id")
        h3["controls"] = controls(profile, h3["controls"])
    return rendered, provenance
