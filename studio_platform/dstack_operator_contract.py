"""Native operator validation and immutable command vocabulary."""

from __future__ import annotations
import math
import os
from pathlib import Path
import stat
from .dstack_capacity import (
    request_from_document,
)
from .capacity_policy import window_open, cold_unadmitted_runtime
from .operator_capacity import (
    OperatorError,
    require,
)
from .repository import (
    request_hash,
)
from .providers.registry import BACKEND_PROVIDERS

_window_open = window_open

_cold_unadmitted_runtime = cold_unadmitted_runtime

_request = request_from_document

BACKEND_MARKER = "dstack-v1"

MAX_HOST_CANDIDATES = 10

# Vast starts by place, staging 10-31 to 11-05: US 9 ready of 11 (both failures one machine),
# Europe 1 of 5. Owner, 2026-11-06: blacklist where machines do not start.
PREFERRED_DOWNLOAD_COUNTRIES = frozenset(
    {"us", "usa", "united states", "united states of america"}
)


def preferred_download_offer(offer):
    country = offer.get('country')
    return (
        isinstance(country, str)
        and country.strip().lower() in PREFERRED_DOWNLOAD_COUNTRIES
    )


PROVIDERS = BACKEND_PROVIDERS

SCHEDULER_GPU_ALIASES = {
    "RTXPRO6000": "RTX PRO 6000 Blackwell",
    "RTXPRO6000S": "RTX PRO 6000 Blackwell Server Edition",
    "RTXPRO6000WS": "RTX PRO 6000 Blackwell Workstation Edition",
}

TERMINAL_JOBS = {"succeeded", "failed", "cancelled"}

OBSERVATION_KEYS = {
    "run_id",
    "run_name",
    "state",
    "ready",
    "reason_code",
    "observed_at",
    "provider_instance_id",
    "runtime_incarnation",
    "dstack_status",
    "model_load_state",
    "billing_state",
    "estimated_cost_microusd",
    "bootstrap_phase",
    "preparation",
    "host_observation",
    "host_failure",
    "no_allocation_proof",
    "provider_removal_proof",
}

IDENTITY_KEYS = {"run_id", "provider_instance_id", "runtime_incarnation"}

REGISTRATION_REASON_CODES = frozenset({"global_capacity_exceeded"})


def _actor(tenant, owner):
    return "dstack:" + request_hash({"tenant_id": tenant, "owner_id": owner})


def _integer(value, minimum=1, maximum=9_000_000_000_000):
    return type(value) is int and minimum <= value <= maximum


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def plan_offers(plan, request):
    """Project pinned 0.22.5 offers; no backend_data, SSH or user/job payload."""
    require(
        isinstance(plan, dict)
        and isinstance(plan.get("job_plans"), list)
        and len(plan["job_plans"]) == 1,
        "dstack_plan_unconfirmed",
    )
    offered = plan["job_plans"][0].get("offers")
    require(isinstance(offered, list), "dstack_plan_unconfirmed")
    normalize = lambda text: "".join(c.lower() for c in text if c.isalnum())
    result = []
    for item in offered:
        if not isinstance(item, dict) or item.get("backend") != request.backend:
            continue
        instance = item.get("instance", {})
        resources = instance.get("resources", {})
        gpus = resources.get("gpus")
        price = item.get("price")
        memory = resources.get("memory_mib")
        disk = resources.get("disk", {}).get("size_mib")
        cpus = resources.get("cpus")
        availability = item.get("availability")
        if (
            not isinstance(gpus, list)
            or len(gpus) != 1
            or not isinstance(gpus[0], dict)
            or not isinstance(gpus[0].get("name"), str)
            or normalize(gpus[0]["name"])
            not in {normalize(name) for name in request.gpu_names}
            or not all(
                _finite(v) and v > 0
                for v in (memory, disk, cpus, gpus[0].get("memory_mib"))
            )
            or memory < request.memory_gib * 1024
            or disk < request.disk_gib * 1024
            or cpus < request.cpu_count
            or gpus[0]["memory_mib"] < request.gpu_memory_gib * 1024
            or not _finite(price)
            or price <= 0
            or math.ceil(price * 1_000_000) > request.max_price_microusd
            or availability not in {"available", "idle", "unknown"}
        ):
            continue
        result.append(
            {
                "backend": request.backend,
                "region": item.get("region"),
                "offer_id": instance.get("name"),
                "gpu_type": gpus[0]["name"],
                "gpu_count": 1,
                "memory_gib": memory / 1024,
                "disk_gib": disk / 1024,
                "gpu_memory_gib": gpus[0]["memory_mib"] / 1024,
                "cpu_count": cpus,
                "hourly_cost_microusd": math.ceil(price * 1_000_000),
                "availability": availability,
                "inventory_confirmed": availability in {"available", "idle"},
            }
        )
    return sorted(
        result,
        key=lambda row: (not row["inventory_confirmed"], row["hourly_cost_microusd"]),
    )[:100]


def _protected_read(filename, *, maximum=262144):
    path = Path(filename)
    require(path.is_absolute(), "dstack_config_path_invalid", 422)
    try:
        with path.open("rb") as stream:
            info = os.fstat(stream.fileno())
            require(
                stat.S_ISREG(info.st_mode)
                and (
                    os.name == "nt" or not info.st_mode & (stat.S_IRWXO | stat.S_IWGRP)
                ),
                "dstack_config_permissions_invalid",
                422,
            )
            raw = stream.read(maximum + 1)
        require(len(raw) <= maximum, "dstack_config_invalid", 422)
        return raw.decode("utf-8")
    except (OSError, UnicodeError):
        raise OperatorError("dstack_config_unavailable", 422) from None


# Preserve the original public import and pickle identity of moved definitions.
preferred_download_offer.__module__ = "studio_platform.dstack_operator"
_actor.__module__ = "studio_platform.dstack_operator"
_integer.__module__ = "studio_platform.dstack_operator"
_finite.__module__ = "studio_platform.dstack_operator"
plan_offers.__module__ = "studio_platform.dstack_operator"
_protected_read.__module__ = "studio_platform.dstack_operator"
