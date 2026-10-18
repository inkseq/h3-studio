"""Pure operator policy for explicit single-slot or two-member services.

This is configuration validation, not an authorization grant, a budget update or
a lease renewal. It has no database, cloud, credential or process dependencies.
Existing finite configurations opt out by omitting ``service_policy``.
"""

from __future__ import annotations

import math
import re


FIELDS = {
    "version",
    "mode",
    "authorization_id",
    "tenant_id",
    "owner_ids",
    "starts_at",
    "expires_at",
    "budget_ceiling_microusd",
    "idle_shutdown_seconds",
    "max_cycles",
}
MODE = "continuing-single-slot"
TWO_MEMBER_MODE = "continuing-two-members"
IDENTIFIER = re.compile(r"[A-Za-z0-9_.:-]{1,120}")
MAX_CYCLE_SEQUENCE = 2**31 - 1
MAX_MONEY = 2**63 - 1


def validate_member_replacement(value):
    """Explicit paid-action limits. Omission never enables replacement."""
    if (
        not isinstance(value, dict)
        or set(value) != {"version", "max_replacements", "backoff_s", "failure_limit"}
        or type(value["version"]) is not int
        or value["version"] != 1
        or type(value["max_replacements"]) is not int
        or not 1 <= value["max_replacements"] <= 16
        or type(value["backoff_s"]) is not int
        or not 60 <= value["backoff_s"] <= 86400
        or type(value["failure_limit"]) is not int
        or not 1 <= value["failure_limit"] <= 5
    ):
        raise ServicePolicyError("service_member_replacement_invalid")
    return dict(value)


class ServicePolicyError(ValueError):
    """Static diagnostic codes only; do not echo operator input."""


def _identifier(value):
    return isinstance(value, str) and IDENTIFIER.fullmatch(value) is not None


def _timestamp(value):
    return type(value) in (int, float) and math.isfinite(value) and 0 < value <= 1e12


def validate_service_policy(value):
    """Validate an explicit finite authority envelope; return a detached copy.

    ``max_cycles=None`` removes the artificial rental-count limit, not the
    absolute expiry, per-node TTL, cumulative ledger or safety spending cap.
    The storage integer bound protects IDs/receipts rather than setting a
    daily test quota. The budget ceiling caps cumulative account spending;
    callers subtract recorded spent/reserved money from the lesser of this cap
    and each existing account limit. It never raises or resets those accounts.
    """
    if not isinstance(value, dict):
        raise ServicePolicyError("service_policy_fields_invalid")
    two_members = value.get("mode") == TWO_MEMBER_MODE
    replacement = (
        {"member_replacement"}
        if two_members and "member_replacement" in value
        else set()
    )
    if set(value) != FIELDS | ({"member_ids"} if two_members else set()) | replacement:
        raise ServicePolicyError("service_policy_fields_invalid")
    if replacement:
        validate_member_replacement(value["member_replacement"])
    if (
        type(value["version"]) is not int
        or value["version"] != 1
        or value["mode"] not in (MODE, TWO_MEMBER_MODE)
        or not _identifier(value["authorization_id"])
        or not _identifier(value["tenant_id"])
    ):
        raise ServicePolicyError("service_policy_identity_invalid")
    if two_members:
        members = value["member_ids"]
        if (
            not isinstance(members, list)
            or len(members) != 2
            or any(not _identifier(v) for v in members)
            or len(set(members)) != 2
            or members != sorted(members)
        ):
            raise ServicePolicyError("service_policy_members_invalid")
    owners = value["owner_ids"]
    if (
        not isinstance(owners, list)
        or not 1 <= len(owners) <= 128
        or any(not _identifier(owner) for owner in owners)
        or len(set(owners)) != len(owners)
    ):
        raise ServicePolicyError("service_policy_owners_invalid")
    if (
        not _timestamp(value["starts_at"])
        or not _timestamp(value["expires_at"])
        or value["starts_at"] >= value["expires_at"]
    ):
        raise ServicePolicyError("service_policy_window_invalid")
    if (
        type(value["budget_ceiling_microusd"]) is not int
        or not 0 < value["budget_ceiling_microusd"] <= MAX_MONEY
    ):
        raise ServicePolicyError("service_policy_budget_invalid")
    if (
        type(value["idle_shutdown_seconds"]) is not int
        or not 1 <= value["idle_shutdown_seconds"] <= 86400
    ):
        raise ServicePolicyError("service_policy_idle_invalid")
    maximum = value["max_cycles"]
    if maximum is not None and (
        type(maximum) is not int or not 1 <= maximum <= MAX_CYCLE_SEQUENCE
    ):
        raise ServicePolicyError("service_policy_cycles_invalid")
    return {
        **value,
        "owner_ids": list(owners),
        **({"member_ids": list(members)} if two_members else {}),
        **(
            {"member_replacement": dict(value["member_replacement"])}
            if replacement
            else {}
        ),
    }


def service_member_ids(config):
    """An explicit operator mode, never inferred from counts or offer IDs."""
    value = getattr(config, "service_policy", None)
    if value is None:
        return ()
    value = validate_service_policy(value)
    return tuple(value["member_ids"]) if value["mode"] == TWO_MEMBER_MODE else ()


def validate_service_config(config):
    """Cross-check the service policy against existing controller fields.

    Return None for legacy configurations. New configurations explicitly mirror
    their scope, absolute window and recommendation limits into the existing
    controller shape; mismatches fail rather than choosing one source. Runtime
    sources, recipe qualification, provider manifests and host identity remain
    the caller's independent validation responsibility.
    """
    raw = getattr(config, "service_policy", None)
    if raw is None:
        return None
    value = validate_service_policy(raw)
    if (
        config.tenant != value["tenant_id"]
        or config.owner not in value["owner_ids"]
        or config.allowed_owners != value["owner_ids"]
    ):
        raise ServicePolicyError("service_policy_scope_mismatch")
    if (
        type(config.authorization_extension_s) is not int
        or config.authorization_extension_s != 0
        or not _timestamp(config.created_at)
        or not _timestamp(config.hard_deadline)
        or config.created_at != value["starts_at"]
        or config.hard_deadline != value["expires_at"]
    ):
        raise ServicePolicyError("service_policy_window_mismatch")
    scale = config.scale_policy
    if (
        not isinstance(scale, dict)
        or not _timestamp(scale.get("hard_deadline"))
        or scale["hard_deadline"] != value["expires_at"]
        or type(scale.get("approved_remaining_microusd")) is not int
        or scale["approved_remaining_microusd"] != value["budget_ceiling_microusd"]
        or type(scale.get("idle_before_drain_s")) not in (int, float)
        or scale["idle_before_drain_s"] != value["idle_shutdown_seconds"]
    ):
        raise ServicePolicyError("service_policy_limits_mismatch")
    count = 2 if value["mode"] == TWO_MEMBER_MODE else 1
    replacement = value.get("member_replacement")
    if (
        replacement is not None
        and config.port_start + 2 * replacement["max_replacements"] + 1 > 65535
    ):
        raise ServicePolicyError("service_member_port_range_invalid")
    if any(
        type(scale.get(field)) is not int or scale[field] != count
        for field in ("max_instances", "max_physical_gpus")
    ) or any(
        type(scale.get(field)) is not int or scale[field] != 1
        for field in ("new_instance_slots", "new_instance_physical_gpus")
    ):
        raise ServicePolicyError("service_policy_single_slot_required")
    if hasattr(config, "max_cycles") and (
        config.max_cycles != value["max_cycles"]
        or config.max_cycles is not None
        and type(config.max_cycles) is not int
    ):
        raise ServicePolicyError("service_policy_cycle_limit_mismatch")
    # Leave room for a '-' and the full bounded integer sequence suffix. The
    # old finite path keeps its old identifier limits and fingerprints. A
    # derived FiniteConfig already contains that suffix and allows 120 chars.
    maximum_length = 109 if hasattr(config, "max_cycles") else 120
    for field in ("cycle_id", "capacity_approval_id"):
        identifier = getattr(config, field)
        if not _identifier(identifier) or len(identifier) > maximum_length:
            raise ServicePolicyError("service_policy_cycle_identifier_invalid")
    return value


def cycle_sequence_allowed(config, sequence):
    """One shared bound for saved receipts, new cycles and rollover decisions."""
    if type(sequence) is not int or not 1 <= sequence <= MAX_CYCLE_SEQUENCE:
        return False
    maximum = config.max_cycles
    if maximum is None:
        policy = validate_service_config(config)
        return policy is not None and policy["max_cycles"] is None
    return type(maximum) is int and sequence <= maximum
