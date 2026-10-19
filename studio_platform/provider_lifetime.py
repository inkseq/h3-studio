"""Read-only lifetime predicate for retained legacy machine records."""

import math


LIFETIME_FRESH_SECONDS = 30


def provider_lifetime_current(payload, intent, now):
    """Shared late-start/worker-admission check of the persisted provider proof."""
    value = payload.get("lifetime", {}) if isinstance(payload, dict) else {}
    return bool(
        value.get("state") == "verified"
        and value.get("instance_id") == intent["provider_instance_id"]
        and type(value.get("observed_at")) in (int, float)
        and math.isfinite(value["observed_at"])
        and 0 <= now - value["observed_at"] <= LIFETIME_FRESH_SECONDS
        and type(value.get("safe_deadline")) in (int, float)
        and math.isfinite(value["safe_deadline"])
        and now < intent["hard_deadline"] <= value["safe_deadline"]
    )
