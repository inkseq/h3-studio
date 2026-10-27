"""Offer-planning headroom; never rewrites runtime profiles or retained requests."""

import copy

from .creator_controls import TAOMATE
from .runtime_catalog import get_profile

GIB = 1024**3
TAOMATE_HEADROOM_GIB = 12


def planning_ram_gib(profile_id, configured):
    if profile_id != TAOMATE:
        return configured
    profile = get_profile(profile_id)
    runtime = profile['runtime']
    required = max(
        profile['minimum_ram_bytes'],
        runtime.get('minimum_available_ram_bytes', 0),
        runtime.get('minimum_total_ram_bytes', 0),
    )
    return max(configured, (required + GIB - 1) // GIB + TAOMATE_HEADROOM_GIB)


def require_preview_ram(document, profile):
    """Reject pre-upgrade previews before any new intent or money reservation.

    Accepted command replays return their original receipt before this boundary.
    The controller continues decoding existing node requests byte-for-byte.
    """
    from .operator_capacity import require

    if profile['runtime_profile_id'] == TAOMATE:
        memory = document.get('memory_gib')
        require(
            type(memory) is int
            and memory >= planning_ram_gib(TAOMATE, profile['memory_gib']),
            'dstack_preview_memory_headroom_stale',
        )
    return document


def configured_ram(profile):
    return planning_ram_gib(profile['runtime_profile_id'], profile['memory_gib'])


def catalog_resources(profile, fields):
    return {
        **{key: copy.deepcopy(profile[key]) for key in fields},
        'memory_gib': configured_ram(profile),
    }


def vast_ram_floor(request):
    # Vast advertises cpu_ram in MiB. Keep historical non-TaoMate mapping intact.
    return request.memory_gib * (1024 if request.profile_id == TAOMATE else 1000)
