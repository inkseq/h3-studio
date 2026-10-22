"""Versioned admission policy over the existing audited admin settings row.

Reading defaults never adopts a policy. Only a confirmed admin write persists
the marker; accepted jobs and rentals retain their original policy snapshot.
"""

import copy
import json
import math

from .repository import request_hash

POLICY_VERSION = 1
DEFAULTS = {
    "capacity_policy_version": POLICY_VERSION,
    "tenant_daily_limit_microusd": 100_000_000,
    "tenant_daily_warning_fraction": 0.8,
    "fleet_hourly_limit_microusd": 3_000_000,
    "mode_hourly_ceiling_microusd": {"ref": 1_500_000, "fl": 850_000, "other": 850_000},
    "max_hourly_price_microusd": 1_500_000,
    "max_machines": 2,
    "idle_shutdown_seconds": 600,
    "max_runtime_seconds": 7200,
    "test_task_limit_microusd": 20_000_000,
    "idle_active_seconds": 1500,
    "idle_quiet_seconds": 300,
    "interactive_activity_window_seconds": 1800,
    "interactive_wait_target_seconds": 180,
    "batch_start_target_seconds": 600,
    "batch_guaranteed_share_slots": {"numerator": 1, "denominator": 4},
    "per_user_machine_share_policy": "equal_active_users",
    "per_user_minimum_machine_share": 1,
}
FIELD_BOUNDS = {
    "tenant_daily_limit_microusd": (1_000_000, 1_000_000_000),
    "tenant_daily_warning_fraction": (0.5, 0.95),
    "fleet_hourly_limit_microusd": (100_000, 40_000_000),
    "max_hourly_price_microusd": (100_000, 5_000_000),
    "max_machines": (1, 8),
    "idle_shutdown_seconds": (1, 86400),
    "max_runtime_seconds": (300, 14400),
    "test_task_limit_microusd": (1_000_000, 20_000_000),
    "idle_active_seconds": (60, 3600),
    "idle_quiet_seconds": (60, 1800),
    "interactive_activity_window_seconds": (60, 7200),
    "interactive_wait_target_seconds": (30, 1800),
    "batch_start_target_seconds": (60, 7200),
    "per_user_minimum_machine_share": (1, 8),
}
LEGACY_FIELDS = frozenset(
    {
        "test_task_limit_microusd",
        "max_hourly_price_microusd",
        "fleet_hourly_limit_microusd",
        "idle_shutdown_seconds",
        "max_runtime_seconds",
    }
)
PENDING_FIELDS = frozenset(
    {
        "idle_active_seconds",
        "idle_quiet_seconds",
        "interactive_activity_window_seconds",
        "interactive_wait_target_seconds",
        "batch_start_target_seconds",
        "batch_guaranteed_share_slots",
        "per_user_machine_share_policy",
        "per_user_minimum_machine_share",
    }
)
DANGEROUS_FIELDS = frozenset(
    {
        "tenant_daily_limit_microusd",
        "fleet_hourly_limit_microusd",
        "max_machines",
        "mode_hourly_ceiling_microusd",
        "max_hourly_price_microusd",
        "test_task_limit_microusd",
    }
)


class CapacitySettingsError(Exception):
    def __init__(self, code, *, status=422, details=None):
        super().__init__(code)
        self.code, self.status, self.details = code, status, details or []


def _invalid(field, **parameters):
    raise CapacitySettingsError(
        "spending_settings_invalid", details=[{"field": field, **parameters}]
    )


def adopted(stored):
    marker = stored["value"].get("capacity_policy_version")
    if marker is None:
        return False
    if type(marker) is not int or marker != POLICY_VERSION:
        _invalid("capacity_policy_version", allowed=[POLICY_VERSION])
    return True


def read_policy(connection, tenant_id, *, admin_defaults=None):
    from .spending_limits import ADMIN_OWNER, read_settings

    stored = read_settings(
        connection, tenant_id, ADMIN_OWNER, admin_defaults=admin_defaults
    )
    return {**stored, "adopted": adopted(stored)}


def admission_snapshot(stored):
    if not adopted(stored):
        return None
    snapshot = {
        "settings_version": stored["version"],
        "capacity_policy_version": POLICY_VERSION,
        "value": copy.deepcopy(stored["value"]),
    }
    validate_snapshot(snapshot)
    return snapshot


def validate_snapshot(snapshot):
    """Validate frozen authority; grandfather only the five original limits."""
    if (
        not isinstance(snapshot, dict)
        or set(snapshot) != {"settings_version", "capacity_policy_version", "value"}
        or type(snapshot["settings_version"]) is not int
        or snapshot["settings_version"] < 1
        or type(snapshot["capacity_policy_version"]) is not int
        or snapshot["capacity_policy_version"] != POLICY_VERSION
        or not isinstance(snapshot["value"], dict)
        or set(snapshot["value"]) != set(DEFAULTS)
    ):
        raise ValueError("capacity_policy_snapshot_invalid")
    value = snapshot["value"]
    try:
        for field in LEGACY_FIELDS:
            minimum, maximum = (1, 9_000_000_000_000_000)
            if field == "idle_shutdown_seconds":
                minimum, maximum = 1, 86400
            elif field == "max_runtime_seconds":
                minimum, maximum = 60, 86400
            _number(field, value[field], minimum, maximum)
        candidate, _ = candidate_value(
            {"value": {key: value[key] for key in LEGACY_FIELDS}}, value
        )
        if not _same(value, candidate):
            raise ValueError("capacity_policy_snapshot_invalid")
    except CapacitySettingsError:
        raise ValueError("capacity_policy_snapshot_invalid") from None
    return copy.deepcopy(value)


def normalize_changes(changes):
    if not isinstance(changes, dict):
        _invalid("changes", type="object")
    result = copy.deepcopy(changes)
    if "test_daily_limit_microusd" in result:
        if "test_task_limit_microusd" in result:
            _invalid("test_daily_limit_microusd")
        result["test_task_limit_microusd"] = result.pop("test_daily_limit_microusd")
    return result


def _number(field, value, minimum, maximum, *, fractional=False):
    valid = type(value) in (int, float) if fractional else type(value) is int
    if not valid or not minimum <= value <= maximum or not math.isfinite(value):
        _invalid(
            field,
            minimum=minimum,
            maximum=maximum,
            type="number" if fractional else "integer",
        )


def _same(left, right):
    try:
        return json.dumps(left, sort_keys=True, allow_nan=False) == json.dumps(
            right, sort_keys=True, allow_nan=False
        )
    except (TypeError, ValueError):
        return False


def candidate_value(stored, changes):
    """Grandfather unchanged stored values; validate every actual change/new field."""
    previous = stored["value"]
    changes = normalize_changes(changes)
    for key, value in changes.items():
        if key not in DEFAULTS and (
            key not in previous or not _same(value, previous[key])
        ):
            _invalid(key, reason="unknown_field")
    candidate = copy.deepcopy(DEFAULTS)
    candidate.update(copy.deepcopy(previous))
    for key, value in changes.items():
        if key in {"mode_hourly_ceiling_microusd", "batch_guaranteed_share_slots"}:
            if not isinstance(value, dict):
                _invalid(key, type="object")
            candidate[key] = {**candidate[key], **value}
        else:
            candidate[key] = value
    candidate["capacity_policy_version"] = POLICY_VERSION
    if "capacity_policy_version" in changes and (
        type(changes["capacity_policy_version"]) is not int
        or changes["capacity_policy_version"] != POLICY_VERSION
    ):
        _invalid("capacity_policy_version", allowed=[POLICY_VERSION])
    changed = sorted(
        key for key in candidate if not _same(candidate[key], previous.get(key))
    )
    for key in changed:
        value = candidate[key]
        if key in FIELD_BOUNDS:
            _number(
                key,
                value,
                *FIELD_BOUNDS[key],
                fractional=key == "tenant_daily_warning_fraction",
            )
        elif key == "mode_hourly_ceiling_microusd":
            if set(value) != {"ref", "fl", "other"}:
                _invalid(key, allowed=["ref", "fl", "other"])
            for mode, amount in value.items():
                _number(key + "." + mode, amount, 100_000, 5_000_000)
        elif key == "batch_guaranteed_share_slots":
            if set(value) != {"numerator", "denominator"}:
                _invalid(key, allowed=["numerator", "denominator"])
            _number(key + ".denominator", value["denominator"], 2, 16)
            _number(key + ".numerator", value["numerator"], 1, value["denominator"])
        elif key == "per_user_machine_share_policy" and value != "equal_active_users":
            _invalid(key, allowed=["equal_active_users"])
    for fields, condition, field in (
        (
            {"mode_hourly_ceiling_microusd"},
            candidate["mode_hourly_ceiling_microusd"]["other"]
            <= min(
                candidate["mode_hourly_ceiling_microusd"]["fl"],
                candidate["mode_hourly_ceiling_microusd"]["ref"],
            ),
            "mode_hourly_ceiling_microusd.other",
        ),
        (
            {"idle_active_seconds", "idle_quiet_seconds"},
            candidate["idle_quiet_seconds"] <= candidate["idle_active_seconds"],
            "idle_quiet_seconds",
        ),
        (
            {"max_machines", "per_user_minimum_machine_share"},
            candidate["per_user_minimum_machine_share"] <= candidate["max_machines"],
            "per_user_minimum_machine_share",
        ),
    ):
        if fields.intersection(changed) and not condition:
            _invalid(field, reason="cross_field_constraint")
    return candidate, changed


def consequences(value, changed_fields, profile_snapshots=None):
    caps = value["mode_hourly_ceiling_microusd"]
    machines = value["max_machines"]
    allowed = [
        min(
            caps.get(p["mode"], caps["other"]),
            p["price_ceiling_microusd"],
            value["max_hourly_price_microusd"],
        )
        for p in (profile_snapshots or {}).values()
    ]
    required = machines * (
        max(allowed)
        if allowed
        else min(max(caps.values()), value["max_hourly_price_microusd"])
    )
    fleet = value["fleet_hourly_limit_microusd"]
    result = [
        {
            "code": "tenant_daily_supplier_limit",
            "limit_microusd": value["tenant_daily_limit_microusd"],
            "warning_fraction": value["tenant_daily_warning_fraction"],
            "timezone": "Australia/Sydney",
        },
        {
            "code": "admitted_work_may_exceed_daily_limit",
            "limit_microusd": value["tenant_daily_limit_microusd"],
        },
    ]
    result.append(
        {
            "code": "fleet_allows_combination",
            "machines": machines,
            "hourly_microusd": required,
        }
        if required <= fleet
        else {
            "code": "fleet_limits_combination",
            "machines": machines,
            "required_hourly_microusd": required,
            "fleet_hourly_limit_microusd": fleet,
        }
    )
    result.extend(
        {"code": "mode_price_ceiling", "mode": mode, "hourly_microusd": caps[mode]}
        for mode in sorted(caps)
    )
    result.extend(
        {
            "code": "mode_ceiling_above_machine_ceiling",
            "mode": mode,
            "mode_hourly_microusd": caps[mode],
            "machine_hourly_microusd": value["max_hourly_price_microusd"],
        }
        for mode in sorted(caps)
        if caps[mode] > value["max_hourly_price_microusd"]
    )
    result.append(
        {"code": "setting_next_admission_only", "fields": sorted(changed_fields)}
    )
    pending = sorted(PENDING_FIELDS.intersection(changed_fields))
    if pending:
        result.append({"code": "control_pending_implementation", "fields": pending})
    return result


def confirmation_digest(
    *, kind, expected_version, changes, profile_snapshots, consequences
):
    return request_hash(
        {
            "kind": kind,
            "expected_version": expected_version,
            "changes": changes,
            "profile_snapshots": profile_snapshots,
            "consequences": consequences,
        }
    )


def preview_change(stored, changes, profile_snapshots):
    value, changed = candidate_value(stored, changes)
    effects = consequences(value, changed, profile_snapshots)
    required = not adopted(stored) or bool(DANGEROUS_FIELDS.intersection(changed))
    return {
        "expected_version": stored["version"],
        "value": value,
        "changed_fields": changed,
        "adopts_policy": not adopted(stored),
        "profile_snapshots": copy.deepcopy(profile_snapshots),
        "consequences": effects,
        "confirmation_required": required,
        "confirmation_digest": confirmation_digest(
            kind="capacity_settings",
            expected_version=stored["version"],
            changes=value,
            profile_snapshots=profile_snapshots,
            consequences=effects,
        ),
    }


def metadata(stored, latest_changes=None):
    bounds = {
        key: {"minimum": limits[0], "maximum": limits[1]}
        for key, limits in FIELD_BOUNDS.items()
    }
    bounds.update(
        {
            "mode_hourly_ceiling_microusd": {
                "fields": {
                    mode: {"minimum": 100_000, "maximum": 5_000_000}
                    for mode in ("ref", "fl", "other")
                }
            },
            "batch_guaranteed_share_slots": {
                "numerator_minimum": 1,
                "denominator_minimum": 2,
                "denominator_maximum": 16,
            },
            "per_user_machine_share_policy": {"allowed": ["equal_active_users"]},
        }
    )
    return {
        "adopted": adopted(stored),
        "adoption_state": "adopted" if adopted(stored) else "legacy_unadopted",
        "capacity_policy_version": POLICY_VERSION if adopted(stored) else None,
        "defaults": copy.deepcopy(DEFAULTS),
        "field_bounds": bounds,
        "controls": {
            key: {"pending_implementation": key in PENDING_FIELDS}
            for key in DEFAULTS
            if key != "capacity_policy_version"
        },
        "latest_changes": latest_changes or {},
    }
