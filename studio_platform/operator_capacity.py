"""Operator capacity commands on the existing database/rental authority.

HTTP writes only durable intent. Providers and boot hooks are trusted controller
dependencies; nothing in this module rents a machine or opens a runtime.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import math
import json
import os
from pathlib import Path
import re
import uuid

from sqlalchemy import (
    Column,
    Float,
    ForeignKey,
    Integer,
    JSON,
    String,
    Table,
    UniqueConstraint,
    func,
    insert,
    or_,
    select,
    update,
)

from .repository import (
    Scope,
    metadata,
    canonical,
    request_hash,
    capacity_gate,
    instance_intents,
    jobs,
    attempts,
    registered_workers,
    registered_devices,
    scaler_actions,
    scaler_receipts,
    paused_capacity_pools,
    manually_reviewed_inactive,
)
from .capacity_types import LaunchSpec, REMOVAL_CHECK_INTERVAL_SECONDS
from .operator_extensions import OperatorExtensions

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


class OperatorCapacity(OperatorExtensions):
    def __init__(self, repo, settings, registry=None):
        self.repo, self.settings, self.registry = (
            repo,
            settings,
            registry or OperatorRegistry(),
        )

    def authorize(self, principal):
        from .identity import require_admin

        return require_admin(
            principal,
            self.repo,
            self.settings,
            legacy_capacity=True,
            legacy_code="operator_forbidden",
        )

    def offers(self, principal, chosen):
        self.authorize(principal)
        chosen = selection(chosen)
        now = self.repo.clock()
        try:
            value = self.registry.offers(chosen, now)
        except Exception:
            value = {
                "status": "unavailable",
                "observed_at": None,
                "offers": [],
                "reason_code": "operator_inventory_unavailable",
            }
        from .capacity_market import market_projection

        with self.repo.engine.connect() as connection:
            market = market_projection(connection, self.registry, chosen, now)
        return {**value, "market": market}

    def _policy(self, connection):
        row = (
            connection.execute(
                select(operator_policy).where(operator_policy.c.id == "global")
            )
            .mappings()
            .first()
        )
        return (
            {"version": row["version"], **row["payload"]}
            if row
            else {"version": 0, **DEFAULT_POLICY}
        )

    def policy(self):
        with self.repo.engine.connect() as connection:
            return self._policy(connection)

    def update_policy(self, principal, body):
        actor = self.authorize(principal)
        require(
            isinstance(body, dict)
            and set(body) == POLICY_FIELDS | {"expected_version"},
            "operator_policy_invalid",
            422,
        )
        require(
            positive_int(body["expected_version"], 2**31 - 1, 0)
            and type(body["enabled"]) is bool
            and positive_int(body["max_instances"], 128, 0)
            and positive_int(body["max_physical_gpus"], 1024, 0)
            and positive_int(body["max_hourly_cost_microusd"], 9_000_000_000_000, 0)
            and positive_int(body["idle_shutdown_seconds"], 86400, 1)
            and positive_int(body["max_ttl_seconds"], 14400, 120),
            "operator_policy_limits_invalid",
            422,
        )
        require(
            not body["enabled"]
            or min(
                body["max_instances"],
                body["max_physical_gpus"],
                body["max_hourly_cost_microusd"],
            )
            > 0,
            "operator_enabled_policy_requires_limits",
            422,
        )
        with self.repo.transaction() as connection:
            gate = self.repo._locked(
                connection, select(capacity_gate).where(capacity_gate.c.id == "global")
            )
            require(gate is not None, "operator_global_capacity_not_initialized")
            row = self.repo._locked(
                connection,
                select(operator_policy).where(operator_policy.c.id == "global"),
            )
            require(
                (row["version"] if row else 0) == body["expected_version"],
                "operator_policy_version_conflict",
            )
            payload = {key: body[key] for key in POLICY_FIELDS}
            value = dict(
                version=body["expected_version"] + 1,
                payload=payload,
                actor=actor,
                updated_at=self.repo.clock(),
            )
            if row:
                connection.execute(
                    update(operator_policy)
                    .where(operator_policy.c.id == "global")
                    .values(**value)
                )
            else:
                connection.execute(insert(operator_policy).values(id="global", **value))
            # Explicit global maximum update; never touches budgets, original TTLs, reservations or jobs.
            connection.execute(
                update(capacity_gate)
                .where(capacity_gate.c.id == "global")
                .values(
                    max_instances=body["max_instances"],
                    max_physical_gpus=body["max_physical_gpus"],
                )
            )
            self.repo._emit(
                connection,
                "operator.capacity.policy_updated",
                "global",
                {"actor": actor, "version": value["version"]},
            )
        return {"policy": {"version": value["version"], **payload}}

    def _committed_capacity(self, connection):
        """Include pending manual commands and real cross-pool resources once.

        The repository's reserve barrier still independently enforces total real
        capacity. An unpriced existing rental blocks a new hourly-cap promise.
        """
        usage = self.repo._global_usage(connection)
        nodes = list(connection.execute(select(operator_nodes)).mappings())
        nodes_by_id = {row["intent_id"]: row for row in nodes}
        hourly = 0
        unknown = False
        known_resources = set()
        for row in connection.execute(
            select(instance_intents).where(instance_intents.c.state != "destroyed")
        ).mappings():
            if manually_reviewed_inactive(connection, row):
                continue
            known_resources.add(
                (row["provider"], row["provider_instance_id"] or "intent:" + row["id"])
            )
            managed = nodes_by_id.get(row["id"])
            if managed is None:
                unknown = True
            else:
                hourly += managed["payload"]["hourly_cost_microusd"]
        unknown = unknown or bool(set(usage["resources"]) - known_resources)
        pending_nodes = pending_gpus = 0
        for cmd in connection.execute(
            select(operator_commands).where(
                operator_commands.c.kind == "start",
                operator_commands.c.state.in_(ACTIVE_COMMANDS),
            )
        ).mappings():
            count = sum(node["command_id"] == cmd["id"] for node in nodes)
            remaining = max(0, cmd["payload"]["selection"]["node_count"] - count)
            pending_nodes += remaining
            pending_gpus += remaining * cmd["payload"]["selection"]["gpu_count"]
            hourly += remaining * cmd["payload"]["hourly_cost_microusd"]
        return {
            "instances": usage["instances"] + pending_nodes,
            "physical_gpus": usage["physical_gpus"] + pending_gpus,
            "allocated_instances": usage["instances"],
            "allocated_physical_gpus": usage["physical_gpus"],
            "hourly": hourly,
            "unpriced": unknown,
        }

    def preview(self, principal, body):
        actor = self.authorize(principal)
        chosen = selection(body)
        blockers = []
        try:
            binding = self.registry.resolve(chosen)
        except OperatorError as error:
            binding = None
            blockers.append({"code": error.code})
        with self.repo.transaction() as connection:
            policy = self._policy(connection)
            now = self.repo.clock()
            usage = self._committed_capacity(connection)
            if not policy["enabled"]:
                blockers.append({"code": "operator_capacity_disabled"})
            if chosen["ttl_seconds"] > policy["max_ttl_seconds"]:
                blockers.append({"code": "operator_ttl_limit"})
            if usage["instances"] + chosen["node_count"] > policy["max_instances"]:
                blockers.append({"code": "operator_instance_limit"})
            if (
                usage["physical_gpus"] + chosen["node_count"] * chosen["gpu_count"]
                > policy["max_physical_gpus"]
            ):
                blockers.append({"code": "operator_gpu_limit"})
            if usage["unpriced"]:
                blockers.append({"code": "operator_unpriced_existing_capacity"})
            hourly = reservation = None
            selected_offer = None
            if binding:
                if binding.pool in paused_capacity_pools(connection):
                    blockers.append({"code": "operator_pool_paused"})
                chosen = {**chosen, "filters": binding.filters}
                hourly = binding.hourly_cost_microusd * chosen["node_count"]
                reservation = (
                    binding.reservation_per_node_microusd * chosen["node_count"]
                )
                if not binding.enabled:
                    blockers.append({"code": "operator_deployment_not_qualified"})
                if chosen["ttl_seconds"] < binding.min_ttl_seconds:
                    blockers.append({"code": "operator_ttl_below_provider_minimum"})
                if now + chosen["ttl_seconds"] > binding.expires_at:
                    blockers.append({"code": "operator_authority_expiring"})
                if usage["hourly"] + hourly > policy["max_hourly_cost_microusd"]:
                    blockers.append({"code": "operator_hourly_cost_limit"})
                if binding.reservation_per_node_microusd < math.ceil(
                    binding.hourly_cost_microusd * chosen["ttl_seconds"] / 3600
                ):
                    blockers.append({"code": "operator_reservation_insufficient"})
                if chosen.get("offer_id"):
                    if (
                        self.registry.inventory_required
                        and not exact_offer_controller_ready(connection, now)
                    ):
                        blockers.append(
                            {"code": "operator_exact_offer_controller_unavailable"}
                        )
                    try:
                        from .capacity_candidates import resolve_selected_offer

                        selected_offer = resolve_selected_offer(
                            connection, self.registry, chosen, now
                        )
                    except OperatorError as error:
                        if {"code": error.code} not in blockers:
                            blockers.append({"code": error.code})
                elif self.registry.inventory_required:
                    inventory = inventory_projection(connection, binding, now)
                    if inventory["status"] != "available":
                        blockers.append({"code": inventory["reason_code"]})
            public = {
                "preview_id": str(uuid.uuid4()),
                "expires_at": now + 120,
                "policy_version": policy["version"],
                "selection": chosen,
                "can_start": not blockers,
                "blockers": blockers,
                "estimated_hourly_cost_microusd": hourly,
                "reservation_microusd": reservation,
                "configuration_id": binding.configuration_id if binding else None,
                "minimum_ttl_seconds": binding.min_ttl_seconds if binding else None,
                "recipe_ids": list(binding.recipe_ids) if binding else [],
            }
            private = {
                **public,
                "binding_id": binding.binding_id if binding else None,
                "binding_hash": command_binding_fingerprint(binding, chosen)
                if binding
                else None,
            }
            if selected_offer is not None:
                # This quote is explicit; the protected ceiling/reservation above
                # remains conservative and is not silently raised by stock.
                public["selected_offer"] = selected_offer
                private["selected_offer"] = selected_offer
                private["offer_fingerprint"] = offer_fingerprint(selected_offer)
            connection.execute(
                insert(operator_previews).values(
                    id=public["preview_id"],
                    actor=actor,
                    payload=private,
                    created_at=now,
                    expires_at=public["expires_at"],
                )
            )
        return public

    def _existing_command(self, connection, actor, key, hashed):
        require(safe_id(key), "operator_idempotency_key_invalid", 422)
        existing = (
            connection.execute(
                select(operator_commands).where(
                    operator_commands.c.actor == actor,
                    operator_commands.c.idempotency_key == key,
                )
            )
            .mappings()
            .first()
        )
        if existing:
            require(existing["request_hash"] == hashed, "operator_idempotency_conflict")
            nodes = list(
                connection.execute(
                    select(operator_nodes).where(
                        operator_nodes.c.command_id == existing["id"]
                    )
                ).mappings()
            )
            result = command_public(existing, nodes)
            if existing["kind"] != "start":
                result["node_ids"] = [existing["payload"]["node_id"]]
            return {"operation": result}

    def start(self, principal, body, key):
        actor = self.authorize(principal)
        require(
            isinstance(body, dict)
            and set(body) == {"preview_id"}
            and safe_id(body["preview_id"]),
            "operator_start_invalid",
            422,
        )
        hashed = request_hash({"kind": "start", "body": body})
        with self.repo.transaction() as connection:
            self.repo._lock_capacity(connection)
            existing = self._existing_command(connection, actor, key, hashed)
            if existing:
                return existing
            consumed = connection.execute(
                select(operator_commands.c.id).where(
                    operator_commands.c.kind == "start",
                    operator_commands.c.payload["preview_id"].as_string()
                    == body["preview_id"],
                )
            ).first()
            require(consumed is None, "operator_preview_already_confirmed")
            row = (
                connection.execute(
                    select(operator_previews).where(
                        operator_previews.c.id == body["preview_id"],
                        operator_previews.c.actor == actor,
                    )
                )
                .mappings()
                .first()
            )
            require(row is not None, "operator_preview_not_found", 404)
            value = row["payload"]
            policy = self._policy(connection)
            now = self.repo.clock()
            require(row["expires_at"] > now, "operator_preview_expired")
            require(value["can_start"], "operator_preview_blocked")
            require(
                policy["enabled"] and policy["version"] == value["policy_version"],
                "operator_policy_changed",
            )
            binding = self.registry.get(value["binding_id"])
            chosen = value["selection"]
            require(
                binding.enabled
                and command_binding_fingerprint(binding, chosen)
                == value["binding_hash"],
                "operator_binding_changed",
            )
            require(
                binding.pool not in paused_capacity_pools(connection),
                "operator_pool_paused",
            )
            if chosen.get("offer_id"):
                require(
                    not self.registry.inventory_required
                    or exact_offer_controller_ready(connection, now),
                    "operator_exact_offer_controller_unavailable",
                )
                from .capacity_candidates import resolve_selected_offer

                offer = resolve_selected_offer(connection, self.registry, chosen, now)
                require(
                    offer_fingerprint(offer) == value.get("offer_fingerprint"),
                    "operator_offer_changed",
                )
            elif self.registry.inventory_required:
                inventory = inventory_projection(connection, binding, now)
                require(inventory["status"] == "available", inventory["reason_code"])
            require(
                chosen["ttl_seconds"] >= binding.min_ttl_seconds,
                "operator_ttl_below_provider_minimum",
            )
            require(
                now + chosen["ttl_seconds"] <= binding.expires_at,
                "operator_authority_expiring",
            )
            usage = self._committed_capacity(connection)
            require(not usage["unpriced"], "operator_unpriced_existing_capacity")
            require(
                usage["instances"] + chosen["node_count"] <= policy["max_instances"],
                "operator_instance_limit",
            )
            require(
                usage["physical_gpus"] + chosen["node_count"] * chosen["gpu_count"]
                <= policy["max_physical_gpus"],
                "operator_gpu_limit",
            )
            require(
                usage["hourly"] + value["estimated_hourly_cost_microusd"]
                <= policy["max_hourly_cost_microusd"],
                "operator_hourly_cost_limit",
            )
            payload = {
                "selection": chosen,
                "preview_id": row["id"],
                "policy_version": policy["version"],
                "binding_id": binding.binding_id,
                "binding_hash": value["binding_hash"],
                "hourly_cost_microusd": binding.hourly_cost_microusd,
                "hard_deadline": now + chosen["ttl_seconds"],
            }
            if chosen.get("offer_id"):
                payload.update(
                    selected_offer=value["selected_offer"],
                    offer_fingerprint=value["offer_fingerprint"],
                )
            command = dict(
                id=str(uuid.uuid4()),
                actor=actor,
                idempotency_key=key,
                request_hash=hashed,
                kind="start",
                state="accepted",
                payload=payload,
                reason_code=None,
                created_at=now,
                updated_at=now,
            )
            connection.execute(insert(operator_commands).values(**command))
            self.repo._emit(
                connection,
                "operator.capacity.start_requested",
                command["id"],
                {"actor": actor, "operation_id": command["id"]},
            )
        return {"operation": command_public(command)}

    def node_command(self, principal, node_id, body, key, kind):
        actor = self.authorize(principal)
        require(
            kind in {"drain", "stop"}
            and safe_id(node_id)
            and isinstance(body, dict)
            and set(body) == {"expected_version"}
            and isinstance(body["expected_version"], str),
            "operator_node_command_invalid",
            422,
        )
        hashed = request_hash({"kind": kind, "node_id": node_id, "body": body})
        with self.repo.transaction() as connection:
            self.repo._lock_capacity(connection)
            existing = self._existing_command(connection, actor, key, hashed)
            if existing:
                return existing
            node = self.repo._locked(
                connection,
                select(operator_nodes).where(operator_nodes.c.intent_id == node_id),
            )
            intent = self.repo._locked(
                connection,
                select(instance_intents).where(instance_intents.c.id == node_id),
            )
            require(
                node is not None and intent is not None, "operator_node_not_found", 404
            )
            require(
                node_version(intent, node) == body["expected_version"],
                "operator_node_version_conflict",
            )
            require(
                not manually_reviewed_inactive(connection, intent),
                "operator_node_manually_reviewed",
            )
            require(intent["state"] != "destroyed", "operator_node_already_destroyed")
            now = self.repo.clock()
            desired = (
                "stopped"
                if kind == "stop" or node["desired_state"] == "stopped"
                else "drained"
            )
            connection.execute(
                update(operator_nodes)
                .where(operator_nodes.c.intent_id == node_id)
                .values(
                    desired_state=desired,
                    payload=advance_command_revision(node["payload"]),
                    updated_at=now,
                )
            )
            command = dict(
                id=str(uuid.uuid4()),
                actor=actor,
                idempotency_key=key,
                request_hash=hashed,
                kind=kind,
                state="accepted",
                payload={
                    "node_id": node_id,
                    "expected_version": body["expected_version"],
                },
                reason_code=None,
                created_at=now,
                updated_at=now,
            )
            connection.execute(insert(operator_commands).values(**command))
            self.repo._emit(
                connection,
                "operator.capacity." + kind + "_requested",
                command["id"],
                {"actor": actor, "node_id": node_id},
            )
        return {"operation": {**command_public(command), "node_ids": [node_id]}}

    def _review_blocker(
        self, connection, intent, node, now, *, lock=False, local_releases=None
    ):
        if intent["provider"] not in {"lium", "targon"}:
            return "operator_manual_review_provider_unsupported"
        if (
            intent["state"]
            not in {
                "creating",
                "creation_unknown",
                "starting",
                "ready",
                "busy",
                "draining",
                "destroying",
            }
            or intent["provider_instance_id"] is None
            and intent["state"] not in {"creating", "creation_unknown"}
            or intent["provider_instance_id"] is not None
            and not safe_id(intent["provider_instance_id"])
        ):
            return "operator_manual_review_instance_unconfirmed"
        if intent["provider_instance_id"] is None:
            # Ambiguous creation has no physical identity to match. Do not
            # invent one or retire actors by guessing: the entire original
            # pool must have no execution/ownership obligations instead.
            worker_query = select(registered_workers).where(
                registered_workers.c.pool == intent["pool"]
            )
            if lock:
                worker_query = worker_query.order_by(
                    registered_workers.c.id
                ).with_for_update()
            workers = list(connection.execute(worker_query).mappings())
            if any(w["state"] != "retired" or w["current_job_id"] for w in workers):
                return "operator_manual_review_worker_active"
            worker_ids = [w["id"] for w in workers]
            device_query = select(registered_devices).where(
                or_(
                    registered_devices.c.worker_id.in_(worker_ids),
                    (registered_devices.c.provider == intent["provider"])
                    & registered_devices.c.instance_id.is_(None),
                )
            )
            if lock:
                device_query = device_query.order_by(
                    registered_devices.c.gpu_id
                ).with_for_update()
            if any(
                d["state"] != "released"
                for d in connection.execute(device_query).mappings()
            ):
                return "operator_manual_review_device_unreleased"
            job_query = select(jobs.c.id, jobs.c.status).where(
                jobs.c.pool == intent["pool"]
            )
            if lock:
                job_query = job_query.order_by(jobs.c.id).with_for_update()
            job_rows = list(connection.execute(job_query).mappings())
            if any(
                j["status"] not in {"succeeded", "failed", "cancelled"}
                for j in job_rows
            ):
                return "operator_manual_review_worker_active"
            attempt_query = select(attempts).where(
                or_(
                    attempts.c.job_id.in_([j["id"] for j in job_rows]),
                    attempts.c.worker_id.in_(worker_ids),
                )
            )
            if lock:
                attempt_query = attempt_query.order_by(attempts.c.id).with_for_update()
            if any(
                h["status"] not in {"succeeded", "failed", "cancelled"}
                or (
                    h["submission_started_at"] is not None
                    or h["upstream_task_id"] is not None
                )
                and h["upstream_stopped"] != 1
                for h in connection.execute(attempt_query).mappings()
            ):
                return "operator_manual_review_attempt_unsafe"
            return None
        # An operator can stop a machine in the provider dashboard. Exact human
        # attestation does not depend on a live retired controller or an earlier
        # platform stop request; local execution obligations still gate closure.
        worker_query = (
            select(registered_workers)
            .where(
                registered_workers.c.provider == intent["provider"],
                registered_workers.c.instance_id == intent["provider_instance_id"],
            )
            .order_by(registered_workers.c.id)
        )
        if lock:
            worker_query = worker_query.with_for_update()
        workers = list(connection.execute(worker_query).mappings())
        if any(
            w["current_job_id"] or w["state"] != "retired" and w["expires_at"] > now
            for w in workers
        ):
            return "operator_manual_review_worker_active"
        worker_ids = [w["id"] for w in workers]
        exact_device = (registered_devices.c.provider == intent["provider"]) & (
            registered_devices.c.instance_id == intent["provider_instance_id"]
        )
        # Include devices owned by these workers outside the exact node too:
        # inconsistent/cross-node ownership must block, never be released.
        device_query = (
            select(registered_devices)
            .where(or_(exact_device, registered_devices.c.worker_id.in_(worker_ids)))
            .order_by(
                registered_devices.c.provider,
                registered_devices.c.instance_id,
                registered_devices.c.gpu_id,
            )
        )
        if lock:
            device_query = device_query.with_for_update()
        devices = list(connection.execute(device_query).mappings())
        if any(
            d["state"] != "released"
            and (
                d["worker_id"] not in worker_ids
                or d["provider"] != intent["provider"]
                or d["instance_id"] != intent["provider_instance_id"]
            )
            for d in devices
        ):
            return "operator_manual_review_device_unreleased"
        history = []
        if workers:
            attempt_query = (
                select(attempts)
                .where(attempts.c.worker_id.in_(worker_ids))
                .order_by(attempts.c.id)
            )
            history = list(connection.execute(attempt_query).mappings())
            job_ids = [h["job_id"] for h in history]
            job_query = (
                select(jobs.c.id, jobs.c.status, jobs.c.lease_worker_id)
                .where(
                    or_(jobs.c.id.in_(job_ids), jobs.c.lease_worker_id.in_(worker_ids))
                )
                .order_by(jobs.c.id)
            )
            if lock:
                job_query = job_query.with_for_update()
            job_rows = {j["id"]: j for j in connection.execute(job_query).mappings()}
            if lock:
                # Follow worker -> job -> attempt locking used by WorkerControl.
                history = list(
                    connection.execute(attempt_query.with_for_update()).mappings()
                )
            if any(
                j["lease_worker_id"] in worker_ids
                and j["status"] not in {"succeeded", "failed", "cancelled"}
                for j in job_rows.values()
            ):
                return "operator_manual_review_worker_active"
            if any(
                h["job_id"] not in job_rows
                or job_rows[h["job_id"]]["status"]
                not in {"succeeded", "failed", "cancelled"}
                or h["status"] not in {"succeeded", "failed", "cancelled"}
                or (
                    h["submission_started_at"] is not None
                    or h["upstream_task_id"] is not None
                )
                and h["upstream_stopped"] != 1
                for h in history
            ):
                return "operator_manual_review_attempt_unsafe"
        release_candidates = [
            w
            for w in workers
            if w["state"] != "retired"
            or any(
                d["worker_id"] == w["id"] and d["state"] != "released" for d in devices
            )
        ]
        if release_candidates:
            from .control import WorkerSpec, worker_spec_payload

            try:
                binding = self.registry.get(node["binding_id"])
                if (
                    binding.fingerprint != node["binding_hash"]
                    or binding.pool != intent["pool"]
                    or binding.launch.provider != intent["provider"]
                ):
                    return "operator_manual_review_worker_binding_unconfirmed"
                for worker in release_candidates:
                    raw = worker["spec"]
                    spec = WorkerSpec(
                        **{
                            **raw,
                            "physical_gpu_ids": tuple(raw["physical_gpu_ids"]),
                            "recipe_ids": tuple(raw["recipe_ids"]),
                        }
                    )
                    if (
                        request_hash(worker_spec_payload(spec)) != worker["spec_hash"]
                        or spec.worker_id != worker["id"]
                        or spec.provider != intent["provider"]
                        or spec.instance_id != intent["provider_instance_id"]
                        or spec.pool != binding.pool
                        or worker["pool"] != binding.pool
                        or spec.backend != "wangp-worker"
                        or spec.model_id != binding.model_id
                        or spec.configuration_id != binding.configuration_id
                        or spec.engine_manifest_digest != binding.engine_manifest_digest
                        or set(spec.recipe_ids) != set(binding.recipe_ids)
                        or worker["state"]
                        not in {
                            "registered",
                            "ready",
                            "busy",
                            "draining",
                            "unknown",
                            "retired",
                        }
                        or not math.isfinite(worker["expires_at"])
                        or worker["expires_at"] > now
                        or type(worker["fence"]) is not int
                        or worker["fence"] < 0
                    ):
                        return "operator_manual_review_worker_binding_unconfirmed"
                    held = [
                        d
                        for d in devices
                        if d["worker_id"] == worker["id"] and d["state"] != "released"
                    ]
                    if any(
                        d["gpu_id"] not in spec.physical_gpu_ids
                        or d["state"] not in {"owned", "reserved"}
                        for d in held
                    ):
                        return "operator_manual_review_device_unreleased"
                    if local_releases is not None:
                        local_releases.append(
                            {
                                "worker_id": worker["id"],
                                "previous_state": worker["state"],
                                "previous_fence": worker["fence"],
                                "next_fence": worker["fence"] + 1,
                                "expires_at": worker["expires_at"],
                                "spec_hash": worker["spec_hash"],
                                "devices": [
                                    {
                                        "gpu_id": d["gpu_id"],
                                        "previous_state": d["state"],
                                    }
                                    for d in held
                                ],
                                "terminal_attempt_ids": [
                                    h["id"]
                                    for h in history
                                    if h["worker_id"] == worker["id"]
                                ],
                            }
                        )
            except (OperatorError, KeyError, TypeError, ValueError):
                return "operator_manual_review_worker_binding_unconfirmed"
        return None

    def manual_review(self, principal, node_id, body, key):
        actor = self.authorize(principal)
        require(
            safe_id(node_id)
            and isinstance(body, dict)
            and set(body)
            == {
                "expected_version",
                "provider_instance_id",
                "account_absent",
                "no_continuing_charge",
            }
            and isinstance(body["expected_version"], str)
            and (
                body["provider_instance_id"] is None
                or safe_id(body["provider_instance_id"])
            )
            and body["account_absent"] is True
            and body["no_continuing_charge"] is True,
            "operator_manual_review_attestation_required",
            422,
        )
        hashed = request_hash(
            {"kind": "manual_review", "node_id": node_id, "body": body}
        )
        with self.repo.transaction() as connection:
            self.repo._lock_capacity(connection)
            existing = self._existing_command(connection, actor, key, hashed)
            if existing:
                return existing
            node = self.repo._locked(
                connection,
                select(operator_nodes).where(operator_nodes.c.intent_id == node_id),
            )
            intent = self.repo._locked(
                connection,
                select(instance_intents).where(instance_intents.c.id == node_id),
            )
            require(
                node is not None and intent is not None, "operator_node_not_found", 404
            )
            require(
                node_version(intent, node) == body["expected_version"],
                "operator_node_version_conflict",
            )
            require(
                body["provider_instance_id"] == intent["provider_instance_id"],
                "operator_manual_review_identity_mismatch",
            )
            require(
                not manually_reviewed_inactive(connection, intent),
                "operator_node_manually_reviewed",
            )
            now = self.repo.clock()
            local_releases = []
            blocker = self._review_blocker(
                connection, intent, node, now, lock=True, local_releases=local_releases
            )
            require(blocker is None, blocker or "operator_manual_review_blocked")
            stop = next(
                (
                    command
                    for command in connection.execute(
                        select(operator_commands)
                        .where(operator_commands.c.kind == "stop")
                        .order_by(operator_commands.c.created_at.desc())
                    ).mappings()
                    if command["payload"].get("node_id") == node_id
                ),
                None,
            )
            command = dict(
                id=str(uuid.uuid4()),
                actor=actor,
                idempotency_key=key,
                request_hash=hashed,
                kind="manual_review",
                state="completed",
                payload={"node_id": node_id, **body},
                reason_code="operator_removal_manually_reviewed",
                created_at=now,
                updated_at=now,
            )
            review = {
                "schema_version": 1,
                "state": "manually_reviewed",
                "intent_id": node_id,
                "instance_id": intent["provider_instance_id"],
                "operation_id": command["id"],
                "actor": actor,
                "account_absent": True,
                "no_continuing_charge": True,
                "deadline": intent["hard_deadline"],
                "provider": intent["provider"],
                "original_intent_state": intent["state"],
                "removal_source": "platform_stop" if stop else "operator_attestation",
                "stop_operation_id": stop["id"] if stop else None,
            }
            if local_releases:
                review["local_execution_release"] = {
                    "state": "local_ownership_released",
                    "provider_removal_confirmed": False,
                    "billing_settled": False,
                    "workers": local_releases,
                }
                for released in local_releases:
                    connection.execute(
                        update(registered_workers)
                        .where(registered_workers.c.id == released["worker_id"])
                        .values(
                            state="retired",
                            current_job_id=None,
                            drain_requested=1,
                            fence=released["next_fence"],
                            updated_at=now,
                        )
                    )
                    for device in released["devices"]:
                        connection.execute(
                            update(registered_devices)
                            .where(
                                registered_devices.c.provider == intent["provider"],
                                registered_devices.c.instance_id
                                == intent["provider_instance_id"],
                                registered_devices.c.worker_id == released["worker_id"],
                                registered_devices.c.gpu_id == device["gpu_id"],
                            )
                            .values(state="released")
                        )
                    self.repo._emit(
                        connection,
                        "operator.capacity.local_worker_retired",
                        released["worker_id"],
                        {
                            "actor": actor,
                            "operation_id": command["id"],
                            "intent_id": node_id,
                            "instance_id": intent["provider_instance_id"],
                            **released,
                        },
                    )
            connection.execute(insert(operator_commands).values(**command))
            connection.execute(
                insert(scaler_receipts).values(
                    id=str(uuid.uuid4()),
                    intent_id=node_id,
                    operation="manual_review",
                    observed_at=now,
                    facts=canonical(review),
                )
            )
            connection.execute(
                update(operator_nodes)
                .where(operator_nodes.c.intent_id == node_id)
                .values(
                    payload={
                        **advance_command_revision(node["payload"]),
                        "manual_review": review,
                    },
                    desired_state="stopped",
                    runtime_state="manually_reviewed",
                    updated_at=now,
                )
            )
            # Existing stop intent is completed by human review, not provider proof.
            for stop in connection.execute(
                select(operator_commands).where(
                    operator_commands.c.kind == "stop",
                    operator_commands.c.state.in_(ACTIVE_COMMANDS),
                )
            ).mappings():
                if stop["payload"].get("node_id") == node_id:
                    connection.execute(
                        update(operator_commands)
                        .where(operator_commands.c.id == stop["id"])
                        .values(
                            state="completed",
                            reason_code="operator_removal_manually_reviewed",
                            updated_at=now,
                        )
                    )
            self.repo._emit(
                connection,
                "operator.capacity.removal_manually_reviewed",
                node_id,
                review,
            )
        return {"operation": {**command_public(command), "node_ids": [node_id]}}

    def state(self, principal):
        actor = self.authorize(principal)
        now = self.repo.clock()
        with self.repo.engine.connect() as connection:
            policy = self._policy(connection)
            controller = (
                connection.execute(
                    select(operator_heartbeats).where(
                        operator_heartbeats.c.id == "global"
                    )
                )
                .mappings()
                .first()
            )
            worker_rows = list(
                connection.execute(select(registered_workers)).mappings()
            )
            from .worker_admission import worker_window_reason

            controller_fresh = bool(
                controller
                and controller["state"] == "running"
                and 0 <= now - controller["observed_at"] <= CONTROLLER_FRESH_SECONDS
            )
            managed = [
                node
                for node in connection.execute(select(operator_nodes)).mappings()
                if node["payload"].get("capacity_backend") != "dstack-v1"
            ]
            intents = {
                row["id"]: row
                for row in connection.execute(select(instance_intents)).mappings()
            }
            node_profiles = {
                (
                    intents[node["intent_id"]]["provider"],
                    intents[node["intent_id"]]["provider_instance_id"],
                ): node["payload"].get("selection", {}).get("runtime_profile_id")
                for node in managed
                if node["intent_id"] in intents
            }
            worker_admission = {
                worker["id"]: worker_window_reason(
                    connection,
                    worker,
                    now,
                    deployment_profile_id=node_profiles.get(
                        (worker["provider"], worker["instance_id"])
                    ),
                )
                for worker in worker_rows
            }
            actions = {
                row["intent_id"]: row
                for row in connection.execute(select(scaler_actions)).mappings()
            }
            commands = list(
                connection.execute(
                    select(operator_commands)
                    .where(
                        ~operator_commands.c.kind.startswith("dstack_", autoescape=True)
                    )
                    .order_by(operator_commands.c.created_at.desc())
                    .limit(100)
                ).mappings()
            )
            removal_checks = dict(
                connection.execute(
                    select(
                        scaler_receipts.c.intent_id,
                        func.max(scaler_receipts.c.observed_at),
                    )
                    .where(scaler_receipts.c.operation == "removal_check_started")
                    .group_by(scaler_receipts.c.intent_id)
                ).all()
            )
            manual_reviews = {
                row["id"]: manually_reviewed_inactive(connection, row)
                for row in intents.values()
            }
            extension_values = {
                row["intent_id"]: self._extension_value(
                    connection, intents[row["intent_id"]], row, 60
                )
                for row in managed
                if row["intent_id"] in intents
            }
            review_blockers = {
                row["intent_id"]: self._review_blocker(
                    connection, intents[row["intent_id"]], row, now
                )
                for row in managed
                if row["intent_id"] in intents
            }
            stop_requests = {}
            for command in connection.execute(
                select(operator_commands)
                .where(operator_commands.c.kind == "stop")
                .order_by(operator_commands.c.created_at.desc())
            ).mappings():
                stop_requests[command["payload"].get("node_id")] = command["created_at"]
            usage = self._committed_capacity(connection)
            running = connection.execute(
                select(func.count())
                .select_from(jobs)
                .where(jobs.c.status.in_(("running", "submitting", "claimed")))
            ).scalar_one()
            waiting = connection.execute(
                select(func.count())
                .select_from(jobs)
                .where(jobs.c.status.in_(("waiting_capacity", "queued")))
            ).scalar_one()
        nodes = []
        for row in managed:
            intent = intents.get(row["intent_id"])
            if not intent:
                continue
            payload = row["payload"]
            observed = actions.get(intent["id"], {}).get("last_observed_at")
            action = actions.get(intent["id"], {})
            requested_at = action.get("destroy_started_at")
            if requested_at is None:
                requested_at = stop_requests.get(intent["id"])
            stale = observed is None or not 0 <= now - observed <= 60
            slots = []
            for worker in worker_rows:
                if (
                    worker["provider"] != intent["provider"]
                    or worker["instance_id"] != intent["provider_instance_id"]
                ):
                    continue
                spec = worker["spec"]
                slots.append(
                    {
                        "id": worker["id"],
                        "gpu_ids": spec["physical_gpu_ids"],
                        "state": worker["state"],
                        "stale": worker["expires_at"] <= now,
                        "current_job_id": worker["current_job_id"],
                        "admission_allowed": bool(
                            controller_fresh
                            and policy["enabled"]
                            and worker["state"] == "ready"
                            and not worker["drain_requested"]
                            and worker["current_job_id"] is None
                            and worker["expires_at"] > now
                            and worker["updated_at"] <= now
                            and worker["spec_hash"] == request_hash(spec)
                            and worker_admission[worker["id"]] is None
                        ),
                        "admission_reason_code": worker_admission[worker["id"]],
                        "configuration_id": spec["configuration_id"],
                        "recipe_ids": spec["recipe_ids"],
                        "engine_manifest_digest": spec.get("engine_manifest_digest"),
                    }
                )
            review = manual_reviews.get(intent["id"])
            allowed = intent["state"] != "destroyed" and not review
            availability = {
                "allowed": allowed,
                "blockers": []
                if allowed
                else [
                    {
                        "code": "operator_node_manually_reviewed"
                        if review
                        else "operator_node_already_destroyed"
                    }
                ],
            }
            review_blocker = (
                "operator_node_manually_reviewed"
                if review
                else review_blockers.get(intent["id"])
            )
            bootstrap = public_bootstrap(payload.get("bootstrap"))
            extension = extension_values[intent["id"]]
            removal = removal_confirmation(
                intent,
                action,
                removal_checks.get(intent["id"]),
                requested_at,
                now,
                review,
            )
            record_group = (
                "history"
                if review or intent["state"] == "destroyed"
                else "pending_review"
                if intent["state"] == "creation_unknown"
                or row["runtime_state"]
                in {"blocked", "failed", "observation_failed", "bootstrap_unconfigured"}
                or removal
                and removal["state"] == "overdue"
                else "current"
            )
            nodes.append(
                {
                    "id": intent["id"],
                    "version": node_version(intent, row),
                    "provider": intent["provider"],
                    "provider_instance_id": intent["provider_instance_id"],
                    "state": intent["state"],
                    "runtime_state": row["runtime_state"],
                    "desired_state": row["desired_state"],
                    "record_group": record_group,
                    "review_required": record_group == "pending_review",
                    "bootstrap": bootstrap,
                    "removal_confirmation": removal,
                    "reason_code": bootstrap.get("reason_code")
                    if bootstrap and row["runtime_state"] in {"blocked", "failed"}
                    else None,
                    "gpu_count": intent["physical_gpus"],
                    "gpu_model": payload["selection"]["gpu_type"],
                    "runtime_profile_id": payload["selection"]["runtime_profile_id"],
                    "observed_at": observed,
                    "stale": stale,
                    "mode": payload["selection"]["mode"],
                    "hourly_cost_microusd": payload["hourly_cost_microusd"],
                    "hourly_cost_basis": "approved_ceiling",
                    "hard_deadline": intent["hard_deadline"],
                    "provider_safe_deadline": payload.get("lifetime", {}).get(
                        "safe_deadline"
                    ),
                    "provider_lifetime_state": payload.get("lifetime", {}).get(
                        "state", "unverified"
                    ),
                    "provider_lifetime_observed_at": payload.get("lifetime", {}).get(
                        "observed_at"
                    ),
                    "last_extension": payload.get("last_extension"),
                    "slots": slots,
                    "actions": {
                        "drain": availability,
                        "stop": availability,
                        "extend": {
                            "allowed": extension["can_extend"],
                            "blockers": extension["blockers"],
                        },
                        "manual_review": {
                            "allowed": review_blocker is None,
                            "blockers": [{"code": review_blocker}]
                            if review_blocker
                            else [],
                        },
                    },
                }
            )
        age = None if controller is None else now - controller["observed_at"]
        return {
            "schema_version": 1,
            "observed_at": now,
            "operator": {
                "account": actor,
                "permissions": {
                    key: True
                    for key in (
                        "view",
                        "start",
                        "drain",
                        "stop",
                        "extend",
                        "manual_review",
                        "update_policy",
                    )
                },
            },
            "policy": policy,
            "controller": {
                "state": controller["state"] if controller else "offline",
                "last_heartbeat_at": controller["observed_at"] if controller else None,
                "stale": age is None or not 0 <= age <= 60,
            },
            "summary": {
                "nodes_active": usage["allocated_instances"],
                "gpus_allocated": usage["allocated_physical_gpus"],
                "slots_ready": sum(
                    slot["admission_allowed"]
                    for node in nodes
                    for slot in node["slots"]
                ),
                "jobs_running": running,
                "jobs_waiting": waiting,
                "hourly_cost_microusd": None if usage["unpriced"] else usage["hourly"],
                "hourly_cost_basis": "approved_ceiling_including_pending_commands",
            },
            "nodes": nodes,
            "operations": [
                command_public(
                    row, [node for node in managed if node["command_id"] == row["id"]]
                )
                if row["kind"] == "start"
                else {**command_public(row), "node_ids": [row["payload"]["node_id"]]}
                for row in commands
            ],
        }
