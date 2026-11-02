"""Original operator tables, deployment bindings and read-only receipt projections."""

from __future__ import annotations
from dataclasses import asdict, dataclass, field
import math
import json
import os
from pathlib import Path
import re
from sqlalchemy import (
    Column,
    Float,
    ForeignKey,
    Integer,
    JSON,
    String,
    Table,
    UniqueConstraint,
    select,
)
from .repository import (
    Scope,
    metadata,
    canonical,
    request_hash,
    instance_intents,
)
from .capacity_types import LaunchSpec, REMOVAL_CHECK_INTERVAL_SECONDS

operator_policy = Table(
    "platform_operator_capacity_policy",
    metadata,
    Column("id", String(30), primary_key=True),
    Column("version", Integer, nullable=False),
    Column("payload", JSON, nullable=False),
    Column("actor", String(200), nullable=False),
    Column("updated_at", Float, nullable=False),
)
operator_previews = Table(
    "platform_operator_capacity_previews",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("actor", String(200), nullable=False),
    Column("payload", JSON, nullable=False),
    Column("created_at", Float, nullable=False),
    Column("expires_at", Float, nullable=False),
)
operator_commands = Table(
    "platform_operator_capacity_commands",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("actor", String(200), nullable=False),
    Column("idempotency_key", String(200), nullable=False),
    Column("request_hash", String(64), nullable=False),
    Column("kind", String(20), nullable=False),
    Column("state", String(30), nullable=False),
    Column("payload", JSON, nullable=False),
    Column("reason_code", String(100)),
    Column("created_at", Float, nullable=False),
    Column("updated_at", Float, nullable=False),
    UniqueConstraint("actor", "idempotency_key"),
)
operator_nodes = Table(
    "platform_operator_capacity_nodes",
    metadata,
    Column(
        "intent_id", String(36), ForeignKey(instance_intents.c.id), primary_key=True
    ),
    Column(
        "command_id", String(36), ForeignKey(operator_commands.c.id), nullable=False
    ),
    Column("ordinal", Integer, nullable=False),
    Column("binding_id", String(200), nullable=False),
    Column("binding_hash", String(64), nullable=False),
    Column("payload", JSON, nullable=False),
    Column("desired_state", String(20), nullable=False),
    Column("runtime_state", String(80), nullable=False),
    Column("updated_at", Float, nullable=False),
    UniqueConstraint("command_id", "ordinal"),
)
operator_heartbeats = Table(
    "platform_operator_capacity_heartbeats",
    metadata,
    Column("id", String(30), primary_key=True),
    Column("controller_id", String(200), nullable=False),
    Column("observed_at", Float, nullable=False),
    Column("state", String(30), nullable=False),
)
operator_inventory = Table(
    "platform_operator_capacity_inventory",
    metadata,
    Column("binding_id", String(200), primary_key=True),
    Column("binding_hash", String(64), nullable=False),
    Column("controller_id", String(200), nullable=False),
    Column("observed_at", Float, nullable=False),
    Column("status", String(30), nullable=False),
    Column("reason_code", String(100)),
)

INVENTORY_FRESH_SECONDS = 120
CONTROLLER_FRESH_SECONDS = 30
REMOVAL_ATTENTION_SECONDS = 300
MANUAL_REVIEW_CONTROLLER_PREFIX = "operator-offers-v1-review-v1-"


def removal_confirmation(intent, action, check_started, requested_at, now, review=None):
    """Read-only progress. A timeout or scheduling claim is never stop proof."""
    if not review and (
        intent["state"] not in {"destroying", "destroyed"} or requested_at is None
    ):
        return None
    action = action or {}
    confirmed = intent["state"] == "destroyed"
    state = (
        "manually_reviewed"
        if review
        else "confirmed"
        if confirmed
        else "overdue"
        if now - requested_at >= REMOVAL_ATTENTION_SECONDS
        else "pending"
    )
    observed = action.get("last_observed_at")
    fact = action.get("last_observation") or {}
    next_check = (
        None
        if confirmed or review
        else max(
            value
            for value in (requested_at, observed, check_started)
            if value is not None
        )
        + REMOVAL_CHECK_INTERVAL_SECONDS
    )
    return {
        "state": state,
        "requested_at": requested_at,
        "last_checked_at": observed,
        "next_check_at": next_check,
        "check_interval_seconds": REMOVAL_CHECK_INTERVAL_SECONDS,
        "attention_after_seconds": REMOVAL_ATTENTION_SECONDS,
        "manual_review": review,
        "reason_code": "operator_removal_manually_reviewed"
        if review
        else None
        if confirmed
        else "provider_removal_confirmation_overdue"
        if state == "overdue"
        else "provider_removal_unconfirmed",
        "last_observation": {
            "state": fact.get("state")
            if fact.get("state")
            in {"unknown", "starting", "running", "destroyed", "not_created"}
            else "unknown",
            "provider_status": fact.get("provider_status")
            if fact.get("provider_status")
            in {"PENDING", "FAILED", "STOPPED", "RUNNING"}
            else None,
            "observed_at": observed,
        },
    }


def inventory_projection(connection, binding, now):
    """Read-only provider observation, bound to this exact protected selection.

    Neither stored prices nor provider response bodies are trusted. The approved
    ceiling comes from the same binding that authorizes a later reservation.
    """
    row = (
        connection.execute(
            select(operator_inventory).where(
                operator_inventory.c.binding_id == binding.binding_id
            )
        )
        .mappings()
        .first()
    )
    heartbeat = (
        connection.execute(
            select(operator_heartbeats).where(operator_heartbeats.c.id == "global")
        )
        .mappings()
        .first()
    )
    fresh = bool(
        row
        and heartbeat
        and row["binding_hash"] == binding.fingerprint
        and row["controller_id"] == heartbeat["controller_id"]
        and heartbeat["state"] in {"running", "degraded"}
        and 0 <= now - heartbeat["observed_at"] <= CONTROLLER_FRESH_SECONDS
        and 0 <= now - row["observed_at"] <= INVENTORY_FRESH_SECONDS
        and binding.enabled
        and now < binding.expires_at
    )
    status = (
        row["status"]
        if fresh and row["status"] in {"available", "unavailable"}
        else "unavailable"
    )
    reason = (
        None
        if status == "available"
        else row["reason_code"]
        if fresh
        and row["reason_code"]
        in {
            "provider_inventory_unavailable",
            "provider_inventory_unconfirmed",
            "operator_inventory_unavailable",
        }
        else "operator_inventory_stale"
    )
    return {
        "status": status,
        "observed_at": row["observed_at"] if row else None,
        "stale": not fresh,
        "offers": [],
        "reason_code": reason,
        "minimum_ttl_seconds": binding.min_ttl_seconds,
        "hourly_cost_microusd": binding.hourly_cost_microusd,
        "hourly_cost_basis": "approved_ceiling",
    }


POLICY_FIELDS = {
    "enabled",
    "max_instances",
    "max_physical_gpus",
    "max_hourly_cost_microusd",
    "idle_shutdown_seconds",
    "max_ttl_seconds",
}
DEFAULT_POLICY = {
    "enabled": False,
    "max_instances": 0,
    "max_physical_gpus": 0,
    "max_hourly_cost_microusd": 0,
    "idle_shutdown_seconds": 600,
    "max_ttl_seconds": 3600,
}
FILTER_FIELDS = {
    "min_ram_gib",
    "min_disk_gib",
    "min_cpu_cores",
    "min_download_mbps",
    "max_price_per_gpu_hour_microusd",
    "allowed_countries",
}
ACTIVE_COMMANDS = {"accepted", "running", "waiting", "unknown"}


class OperatorError(ValueError):
    def __init__(self, code, status=409, *, details=None):
        self.code, self.status = code, status
        self.details = details
        super().__init__(code)


def require(condition, code, status=409):
    if not condition:
        raise OperatorError(code, status)


def safe_id(value):
    return (
        isinstance(value, str)
        and re.fullmatch(r"[A-Za-z0-9_.:\-]{1,200}", value) is not None
    )


def positive_int(value, maximum, minimum=1):
    return type(value) is int and minimum <= value <= maximum


def command_binding_fingerprint(binding, chosen):
    # Old controllers compare this field with the unversioned binding digest.
    # Exact selection therefore fails closed even after a rollback/lease move.
    if chosen.get("offer_id"):
        return request_hash(
            {"binding": binding.fingerprint, "protocol": "operator-exact-offer-v1"}
        )
    return binding.fingerprint


def offer_fingerprint(offer):
    # Availability is rechecked, but another allocation of a Targon SKU does
    # not change the operator's selected hardware/price. Observation age is
    # checked separately, never included in the immutable quote identity.
    return request_hash(
        {
            key: value
            for key, value in offer.items()
            if key not in {"available_count", "available_gpu_count", "observed_at"}
        }
    )


def exact_offer_controller_ready(connection, now):
    heartbeat = (
        connection.execute(
            select(operator_heartbeats).where(operator_heartbeats.c.id == "global")
        )
        .mappings()
        .first()
    )
    return bool(
        heartbeat
        and heartbeat["controller_id"].startswith("operator-offers-v1-")
        and heartbeat["state"] in {"running", "degraded"}
        and 0 <= now - heartbeat["observed_at"] <= CONTROLLER_FRESH_SECONDS
    )


def selection(value):
    required = {
        "runtime_profile_id",
        "mode",
        "gpu_type",
        "node_count",
        "gpu_count",
        "ttl_seconds",
    }
    require(
        isinstance(value, dict)
        and required <= set(value)
        and not set(value) - required - {"filters", "provider", "offer_id"},
        "operator_selection_invalid",
        422,
    )
    # Keep absent provider absent: historical selection hashes/replays remain exact.
    require(
        value.get("provider", "lium") in ("lium", "targon"),
        "operator_provider_invalid",
        422,
    )
    require(
        safe_id(value["runtime_profile_id"])
        and isinstance(value["gpu_type"], str)
        and 1 <= len(value["gpu_type"]) <= 120
        and not any(ord(c) < 32 for c in value["gpu_type"]),
        "operator_selection_identity_invalid",
        422,
    )
    require(
        positive_int(value["node_count"], 32)
        and positive_int(value["gpu_count"], 8)
        and positive_int(value["ttl_seconds"], 14400, 120),
        "operator_selection_limits_invalid",
        422,
    )
    require(value["mode"] in ("fl", "ref"), "operator_mode_invalid", 422)
    if "offer_id" in value:
        require(
            safe_id(value["offer_id"]) and value["node_count"] == 1,
            "operator_exact_offer_invalid",
            422,
        )
    filters = value.get("filters", {})
    require(
        isinstance(filters, dict) and not set(filters) - FILTER_FIELDS,
        "operator_filters_invalid",
        422,
    )
    for key, item in filters.items():
        if key == "allowed_countries":
            require(
                isinstance(item, list)
                and len(item) <= 30
                and all(
                    isinstance(x, str) and re.fullmatch(r"[A-Z]{2}", x) for x in item
                )
                and len(set(item)) == len(item),
                "operator_countries_invalid",
                422,
            )
        else:
            require(
                positive_int(
                    item, 9_000_000_000_000 if "microusd" in key else 1_000_000
                ),
                "operator_filter_number_invalid",
                422,
            )
    return canonical({**value, "filters": filters})


@dataclass(frozen=True)
class DeploymentBinding:
    """Trusted deployment recipe/host policy; never constructed from HTTP JSON."""

    binding_id: str
    runtime_profile_id: str
    gpu_type: str
    gpu_count: int
    pool: str
    configuration_id: str
    model_id: str
    recipe_ids: tuple[str, ...]
    engine_manifest_digest: str
    launch: LaunchSpec
    scope: Scope
    budget_account_ids: tuple[str, ...]
    hourly_cost_microusd: int
    reservation_per_node_microusd: int
    expires_at: float
    max_ttl_seconds: int = 3600
    min_ttl_seconds: int = 120
    execution_slots: int = 1
    filters: dict = field(default_factory=dict)
    enabled: bool = False
    # Trusted references only; never returned in HTTP projections.
    boot: dict = field(default_factory=dict)

    def __post_init__(self):
        for value in (
            self.binding_id,
            self.runtime_profile_id,
            self.pool,
            self.configuration_id,
            self.model_id,
            *self.recipe_ids,
            *self.budget_account_ids,
        ):
            require(safe_id(value), "operator_binding_identity_invalid", 422)
        require(
            self.launch.configuration_id == self.configuration_id
            and self.launch.model_id == self.model_id
            and self.launch.provider in {"lium", "targon"}
            and bool(self.recipe_ids)
            and bool(self.budget_account_ids)
            and self.recipe_ids in (("h3-base-fl2va-v1",), ("h3-base-ref2va-v1",)),
            "operator_binding_contract_invalid",
            422,
        )
        require(
            isinstance(self.engine_manifest_digest, str)
            and re.fullmatch(r"[0-9a-f]{64}", self.engine_manifest_digest),
            "operator_binding_manifest_invalid",
            422,
        )
        require(
            positive_int(self.gpu_count, 8)
            and positive_int(self.execution_slots, self.gpu_count)
            and positive_int(self.max_ttl_seconds, 14400, 120)
            and positive_int(self.min_ttl_seconds, self.max_ttl_seconds, 120)
            and positive_int(self.hourly_cost_microusd, 9_000_000_000_000)
            and positive_int(self.reservation_per_node_microusd, 9_000_000_000_000)
            and type(self.enabled) is bool
            and type(self.expires_at) in (int, float)
            and math.isfinite(self.expires_at),
            "operator_binding_limits_invalid",
            422,
        )
        selection(
            {
                "runtime_profile_id": self.runtime_profile_id,
                "mode": self.mode,
                "gpu_type": self.gpu_type,
                "node_count": 1,
                "gpu_count": self.gpu_count,
                "ttl_seconds": self.max_ttl_seconds,
                "filters": self.filters,
            }
        )
        require(isinstance(self.boot, dict), "operator_boot_binding_invalid", 422)

    @property
    def fingerprint(self):
        # Disable new starts without invalidating immutable existing cleanup.
        value = asdict(self)
        value.pop("enabled")
        return request_hash(value)

    @property
    def mode(self):
        return "fl" if self.recipe_ids == ("h3-base-fl2va-v1",) else "ref"

    def matches(self, chosen):
        return (
            self.runtime_profile_id == chosen["runtime_profile_id"]
            and self.gpu_type == chosen["gpu_type"]
            and self.mode == chosen["mode"]
            and self.launch.provider == chosen.get("provider", "lium")
            and self.gpu_count == chosen["gpu_count"]
            and chosen["ttl_seconds"] <= self.max_ttl_seconds
            and (not chosen["filters"] or chosen["filters"] == self.filters)
        )


class OperatorRegistry:
    """Server-owned registry seam. Optional resolver MUST retain immutable IDs.

    Dynamic resolver is for trusted pre-approved provider manifests, not a
    browser-provided launch payload. ``get`` must resolve accepted bindings after
    restarts, including disabled/expired ones needed for cleanup.
    """

    def __init__(
        self,
        bindings=(),
        *,
        catalog=None,
        offers=None,
        resolver=None,
        getter=None,
        inventory_required=False,
        qualified_providers=("lium",),
    ):
        values = tuple(bindings)
        require(
            all(isinstance(x, DeploymentBinding) for x in values)
            and len({x.binding_id for x in values}) == len(values),
            "operator_registry_invalid",
            422,
        )
        self.bindings = {item.binding_id: item for item in values}
        self.catalog_reader, self.offers_reader = catalog, offers
        self.resolver, self.getter = resolver, getter
        self.inventory_required = inventory_required
        require(
            isinstance(qualified_providers, (tuple, list, set, frozenset))
            and all(
                isinstance(value, str) and value in {"lium", "targon"}
                for value in qualified_providers
            ),
            "operator_registry_providers_invalid",
            422,
        )
        self.qualified_providers = frozenset(qualified_providers)

    def resolve(self, chosen):
        # Inventory support does not qualify a VM's paid lifecycle/bootstrap.
        require(
            chosen.get("provider", "lium") in self.qualified_providers,
            "operator_provider_start_unqualified",
        )
        if self.resolver is not None:
            result = self.resolver(chosen)
            require(
                isinstance(result, DeploymentBinding) and result.matches(chosen),
                "operator_binding_selection_mismatch",
            )
            return result
        values = [item for item in self.bindings.values() if item.matches(chosen)]
        # Retired bindings remain addressable by get() for their paid ledger.
        # New selections prefer the unique enabled successor, never insertion order.
        enabled = [item for item in values if item.enabled]
        if enabled:
            values = enabled
        require(len(values) == 1, "operator_deployment_not_configured")
        return values[0]

    def get(self, binding_id):
        result = (
            self.getter(binding_id)
            if self.getter is not None
            else self.bindings.get(binding_id)
        )
        require(
            isinstance(result, DeploymentBinding) and result.binding_id == binding_id,
            "operator_binding_unavailable",
        )
        return result

    def catalog(self):
        return (
            self.catalog_reader()
            if self.catalog_reader
            else {"schema_version": 1, "profiles": [], "gpu_types": []}
        )

    def offers(self, chosen, now):
        if self.offers_reader is None:
            return {
                "status": "unconfigured",
                "observed_at": now,
                "offers": [],
                "reason_code": "operator_inventory_not_configured",
            }
        # Integration supplies an explicitly redacted projection, never provider raw JSON.
        return self.offers_reader(chosen)

    @classmethod
    def from_file(cls, path, *, catalog=None, offers=None):
        """Read a server-owned file, never an HTTP path or executable import.

        This is deployment configuration, not a credential store. Boot/launch
        contain paths and identifiers referencing protected configuration only.
        Keep retired bindings in this file until their rental ledger is settled.
        """
        source = Path(path)
        require(
            source.is_absolute() and source.is_file(),
            "operator_registry_file_invalid",
            422,
        )
        require(
            source.stat().st_size <= 1024 * 1024,
            "operator_registry_file_too_large",
            422,
        )
        try:
            value = json.loads(source.read_text(encoding="utf-8"))
            require(
                isinstance(value, dict)
                and set(value) == {"schema_version", "bindings"}
                and type(value["schema_version"]) is int
                and value["schema_version"] == 1
                and isinstance(value["bindings"], list)
                and len(value["bindings"]) <= 128,
                "operator_registry_schema_invalid",
                422,
            )
            bindings = []
            for entry in value["bindings"]:
                require(
                    isinstance(entry, dict), "operator_registry_binding_invalid", 422
                )
                item = dict(entry)
                item["launch"] = LaunchSpec(**item["launch"])
                item["scope"] = Scope(**item["scope"])
                for key in ("recipe_ids", "budget_account_ids"):
                    require(
                        isinstance(item[key], list),
                        "operator_registry_binding_invalid",
                        422,
                    )
                    item[key] = tuple(item[key])
                bindings.append(DeploymentBinding(**item))
            return cls(bindings, catalog=catalog, offers=offers)
        except OperatorError:
            raise
        except (KeyError, TypeError, ValueError, OSError):
            raise OperatorError("operator_registry_file_invalid", 422) from None

    @classmethod
    def from_environment(cls, *, catalog=None, offers=None, repository=None):
        runtime = os.environ.get("H3_OPERATOR_RUNTIME_CONFIG", "").strip()
        source = os.environ.get("H3_OPERATOR_CAPACITY_REGISTRY", "").strip()
        require(
            not (runtime and source), "operator_registry_configuration_conflict", 422
        )
        if runtime:
            raise OperatorError("legacy_capacity_runtime_retired", 410)
        return (
            cls.from_file(source, catalog=catalog, offers=offers)
            if source
            else cls(catalog=catalog, offers=offers)
        )


def command_public(row, nodes=()):
    return {
        "id": row["id"],
        "kind": row["kind"],
        "state": row["state"],
        "reason_code": row["reason_code"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "node_ids": [n["intent_id"] for n in nodes],
        "selection": row["payload"].get("selection"),
        **(
            {"extension": row["payload"]["extension"]}
            if row["kind"] == "extend"
            else {}
        ),
    }


def command_revision(payload):
    """Control CAS token; legacy rows read as zero without backfilling on GET."""
    value = payload.get("command_revision", 0)
    require(type(value) is int and value >= 0, "operator_node_revision_invalid")
    return value


def advance_command_revision(payload):
    """Call only for an accepted control write under the original node lock."""
    return {**payload, "command_revision": command_revision(payload) + 1}


def node_version(intent, node):
    return request_hash(
        {
            "id": intent["id"],
            "state": intent["state"],
            "provider_instance_id": intent["provider_instance_id"],
            "updated_at": intent["updated_at"],
            "desired_state": node["desired_state"],
            "hard_deadline": intent["hard_deadline"],
            **(
                {"manual_review": node["payload"]["manual_review"]}
                if node["payload"].get("manual_review")
                else {}
            ),
        }
    )


def public_bootstrap(value):
    """Small, static projection; never expose remote logs, paths or messages."""
    if not isinstance(value, dict):
        return None
    from .native_bootstrap import _static, safe_bootstrap_diagnosis

    states = {
        "ready",
        "preparing",
        "starting",
        "waiting",
        "blocked",
        "failed",
        "draining",
        "stopped",
        "recovering",
    }
    slot_states = {
        "fleet_running",
        "draining",
        "bootstrap_failed",
        "staging_failed",
        "fleet_attention_required",
        "fleet_recovery_required",
        "staging_recovery_required",
        "bootstrap_start_unknown",
        "bootstrap_reconciliation_required",
        "booting",
        "staging",
        "staged",
        "bootstrap_locked",
        "staging_cancelled",
        "staging_authority_unavailable",
        "bootstrap_start_not_authorized",
        "bootstrap_deadline_insufficient",
        "instance_not_admitting",
        "instance_not_confirmed",
        "ready_for_qualification",
        "runtime_ready",
        "qualified",
        "unknown",
    }
    result = {
        "state": _static(value.get("state"), states, "blocked"),
        "reason_code": _static(
            value.get("reason_code"),
            {"bootstrap_reconciliation_required", "operator_bootstrap_failed"},
            None,
        ),
        "slots": [],
    }
    observed = value.get("observed_at")
    if type(observed) in (int, float) and math.isfinite(observed):
        result["observed_at"] = observed
    slots = value.get("slots", [])
    for index, slot in enumerate(slots[:8] if isinstance(slots, list) else []):
        if not isinstance(slot, dict):
            continue
        safe = safe_bootstrap_diagnosis(slot)
        item = {
            "index": index,
            "state": _static(slot.get("state"), slot_states, "unknown"),
        }
        item.update(
            {
                key: safe[key]
                for key in ("phase", "failure_phase", "error_code", "error_type")
                if key in slot
            }
        )
        result["slots"].append(item)
    return result


# Preserve the original public import and pickle identity of moved definitions.
removal_confirmation.__module__ = "studio_platform.operator_capacity"
inventory_projection.__module__ = "studio_platform.operator_capacity"
OperatorError.__module__ = "studio_platform.operator_capacity"
require.__module__ = "studio_platform.operator_capacity"
safe_id.__module__ = "studio_platform.operator_capacity"
positive_int.__module__ = "studio_platform.operator_capacity"
command_binding_fingerprint.__module__ = "studio_platform.operator_capacity"
offer_fingerprint.__module__ = "studio_platform.operator_capacity"
exact_offer_controller_ready.__module__ = "studio_platform.operator_capacity"
selection.__module__ = "studio_platform.operator_capacity"
DeploymentBinding.__module__ = "studio_platform.operator_capacity"
OperatorRegistry.__module__ = "studio_platform.operator_capacity"
command_public.__module__ = "studio_platform.operator_capacity"
command_revision.__module__ = "studio_platform.operator_capacity"
advance_command_revision.__module__ = "studio_platform.operator_capacity"
node_version.__module__ = "studio_platform.operator_capacity"
public_bootstrap.__module__ = "studio_platform.operator_capacity"
