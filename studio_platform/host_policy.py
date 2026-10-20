"""Typed host evidence and policy on the original operator records.

No provider effects or new accounting/telemetry store. Only positively bound
physical identity participates in automatic policy; legacy unknowns stay unknown.
"""

from __future__ import annotations

import copy
import math
import re

from sqlalchemy import select

from .operator_capacity import operator_nodes, operator_policy, require
from .repository import request_hash

MAX_NODES = 4096
MAX_OFFERS = 1000
FRESH_SECONDS = 120
POLICIES = {"default", "prefer", "deny"}
REASONS = {
    "default": "operator_host_default",
    "prefer": "operator_host_preferred",
    "deny": "operator_host_denied",
}
START_FAILURES = {
    "provider_provisioning_timeout",
    "provider_image_pull_failed",
    "provider_runner_timeout",
}
HOST_FIELDS = {
    "version",
    "provider",
    "machine_id",
    "provider_instance_id",
    "offer_id",
    "observed_at",
    "region",
    "gpu",
    "gpu_count",
    "allocated_ram_mib",
    "allocated_vram_mib",
    "cpu",
    "disk_mib",
    "reliability",
    "quoted_hourly_microusd",
    "identity_confirmed",
    "physical_id_source",
}
OFFER_METADATA_FIELDS = {"country", "region", "advertised_download_mbps"}
HOST_METADATA_FIELDS = OFFER_METADATA_FIELDS - {"region"}


def finite(value, *, minimum=0, maximum=9_000_000_000_000):
    return (
        type(value) in (int, float)
        and math.isfinite(value)
        and minimum <= value <= maximum
    )


def numeric_id(value):
    require(
        isinstance(value, str) and re.fullmatch(r"[1-9][0-9]{0,17}", value),
        "host_identity_invalid",
        422,
    )
    return value


def provider_id(value):
    if type(value) is int and value > 0:
        value = str(value)
    return numeric_id(value)


def offer_metadata(value):
    """Closed advertised facts, never wire throughput or inferred geography."""
    require(
        isinstance(value, dict) and set(value) <= OFFER_METADATA_FIELDS,
        "host_offer_metadata_invalid",
    )
    country, region, bandwidth = (
        value.get(key) for key in ("country", "region", "advertised_download_mbps")
    )
    require(
        country is None
        or isinstance(country, str)
        and re.fullmatch(r"[A-Za-z][A-Za-z .'-]{0,99}", country),
        "host_offer_metadata_invalid",
    )
    require(
        region is None
        or isinstance(region, str)
        and re.fullmatch(r"[a-z0-9-]{1,100}", region),
        "host_offer_metadata_invalid",
    )
    require(
        bandwidth is None or finite(bandwidth, maximum=1_000_000_000),
        "host_offer_metadata_invalid",
    )
    return {"country": country, "region": region, "advertised_download_mbps": bandwidth}


def observation(value, *, now):
    require(
        isinstance(value, dict)
        and HOST_FIELDS <= set(value) <= HOST_FIELDS | HOST_METADATA_FIELDS
        and type(value["version"]) is int
        and value["version"] == 1
        and value["provider"] == "vastai"
        and value["identity_confirmed"] is True
        and value["physical_id_source"] == "vast_machine_id",
        "host_observation_invalid",
    )
    for name in ("machine_id", "provider_instance_id", "offer_id"):
        numeric_id(value[name])
    require(
        finite(value["observed_at"], minimum=1, maximum=now), "host_observation_invalid"
    )
    require(
        value["region"] is None
        or isinstance(value["region"], str)
        and re.fullmatch(r"[a-z0-9-]{1,100}", value["region"]),
        "host_observation_invalid",
    )
    require(
        isinstance(value["gpu"], str)
        and re.fullmatch(r"[A-Za-z0-9 _.()-]{1,100}", value["gpu"])
        and value["gpu_count"] == 1
        and type(value["gpu_count"]) is int,
        "host_observation_invalid",
    )
    for name in ("allocated_ram_mib", "allocated_vram_mib", "cpu", "disk_mib"):
        require(finite(value[name], minimum=1), "host_observation_invalid")
    require(
        value["reliability"] is None or finite(value["reliability"], maximum=1),
        "host_observation_invalid",
    )
    require(
        type(value["quoted_hourly_microusd"]) is int
        and 0 < value["quoted_hourly_microusd"] <= 9_000_000_000_000,
        "host_observation_invalid",
    )
    offer_metadata({key: value[key] for key in OFFER_METADATA_FIELDS if key in value})
    return copy.deepcopy(value)


def bound_observation(node, *, now):
    payload = node.get("payload", {})
    value = payload.get("host_observation") if isinstance(payload, dict) else None
    if value is None:
        return None
    try:
        value = observation(value, now=now)
        bound = payload["dstack"]
        require(
            payload.get("capacity_backend") == "dstack-v1"
            and bound.get("backend") == "vastai"
            and bound.get("provider_instance_id") == value["provider_instance_id"],
            "host_observation_binding_invalid",
        )
        return value
    except (ValueError, KeyError):
        return None


def failure(value, *, now):
    require(
        isinstance(value, dict)
        and set(value)
        == {"version", "stage", "reason_code", "at", "qualified_start_failure"}
        and value["version"] == 1
        and type(value["version"]) is int
        and (
            value["stage"] in {"provisioning", "image_pull", "runtime_start"}
            and value["reason_code"] in START_FAILURES
            and value["qualified_start_failure"] is True
            or value["stage"] == "model_download"
            and value["reason_code"] == "dstack_download_timebox_exceeded"
            and value["qualified_start_failure"] is False
        )
        and finite(value["at"], minimum=1, maximum=now),
        "host_failure_invalid",
    )
    return copy.deepcopy(value)


def original_nodes(connection, tenant, *, owner=None):
    query = select(operator_nodes).where(
        operator_nodes.c.payload["tenant_id"].as_string() == tenant,
        operator_nodes.c.payload["capacity_backend"].as_string() == "dstack-v1",
    )
    if owner is not None:
        query = query.where(operator_nodes.c.payload["owner_id"].as_string() == owner)
    rows = [
        dict(row) for row in connection.execute(query.limit(MAX_NODES + 1)).mappings()
    ]
    require(len(rows) <= MAX_NODES, "host_history_limit_exceeded")
    return rows


def automatic_deny(nodes, machine_id, *, now):
    """Derive fixed seven-day intervals from distinct original failed rentals.

    A stopped-by-owner row, a model-load problem, missing identity, or repeated
    observations of one rental can never count as a second startup failure.
    """
    events = []
    for node in nodes:
        physical = bound_observation(node, now=now)
        payload = node.get("payload", {})
        if not physical or physical["machine_id"] != machine_id:
            continue
        try:
            item = failure(payload.get("host_failure"), now=now)
            if item["qualified_start_failure"] and item["at"] >= now - 8 * 86400:
                events.append((item["at"], node["intent_id"]))
        except (ValueError, KeyError):
            pass
    events = sorted(set(events))
    expires = None
    for index, (at, _) in enumerate(events):
        if index and at - events[index - 1][0] <= 86400:
            until = at + 7 * 86400
            if now < until:
                expires = max(expires or 0, until)
    return expires


class HostPolicyStore:
    def __init__(self, repo, tenant):
        self.repo, self.tenant = repo, tenant

    def key(self, machine_id):
        numeric_id(machine_id)
        # The original shared operator_policy.id is VARCHAR(30).
        return (
            "host:"
            + request_hash(
                {"tenant": self.tenant, "provider": "vastai", "machine_id": machine_id}
            )[:25]
        )

    def read(self, connection, machine_id, *, nodes=None, now=None):
        numeric_id(machine_id)
        now = self.repo.clock() if now is None else now
        row = (
            connection.execute(
                select(operator_policy).where(
                    operator_policy.c.id == self.key(machine_id)
                )
            )
            .mappings()
            .first()
        )
        value = {
            "version": 1,
            "tenant_id": self.tenant,
            "provider": "vastai",
            "machine_id": machine_id,
            "manual": "default",
            "reason_code": None,
        }
        revision = 0
        if row:
            stored = row["payload"]
            require(
                isinstance(stored, dict)
                and set(stored) == set(value)
                and all(
                    stored[key] == value[key]
                    for key in ("version", "tenant_id", "provider", "machine_id")
                )
                and stored["manual"] in POLICIES
                and stored["reason_code"] == REASONS[stored["manual"]]
                and type(row["version"]) is int
                and row["version"] >= 1,
                "host_policy_binding_invalid",
            )
            value, revision = copy.deepcopy(stored), row["version"]
        nodes = original_nodes(connection, self.tenant) if nodes is None else nodes
        until = automatic_deny(nodes, machine_id, now=now)
        effective = (
            "deny"
            if value["manual"] == "deny" or until is not None
            else value["manual"]
        )
        return {
            "manual": value["manual"],
            "effective": effective,
            "revision": revision,
            "snapshot": request_hash({"revision": revision, "value": value}),
            "reason_code": (
                "automatic_start_failures"
                if until is not None and value["manual"] != "deny"
                else value["reason_code"]
            ),
            "expires_at": until
            if effective == "deny" and value["manual"] != "deny"
            else None,
            "automatic_deny_until": until,
        }, value

    def known_policy_ids(self, connection):
        rows = list(
            connection.execute(
                select(operator_policy.c.payload)
                .where(
                    operator_policy.c.id.like("host:%"),
                    operator_policy.c.payload["tenant_id"].as_string() == self.tenant,
                )
                .limit(MAX_NODES + 1)
            ).scalars()
        )
        require(len(rows) <= MAX_NODES, "host_history_limit_exceeded")
        return {
            numeric_id(row["machine_id"])
            for row in rows
            if isinstance(row, dict) and row.get("provider") == "vastai"
        }

    def admission_snapshot(self, connection, offers, *, now=None):
        require(
            isinstance(offers, (list, tuple)) and 0 < len(offers) <= MAX_OFFERS,
            "host_selection_invalid",
        )
        nodes = original_nodes(connection, self.tenant)
        policies = {}
        for item in offers:
            require(isinstance(item, dict), "host_selection_invalid")
            machine = numeric_id(item["machine_id"])
            if machine not in policies:
                policies[machine] = self.read(
                    connection, machine, nodes=nodes, now=now
                )[0]
            require(policies[machine]["effective"] != "deny", "host_policy_denied")
        return request_hash(
            {
                machine: {
                    key: value[key]
                    for key in (
                        "manual",
                        "effective",
                        "revision",
                        "snapshot",
                        "automatic_deny_until",
                    )
                }
                for machine, value in sorted(policies.items())
            }
        )


def selection(value, *, now=None):
    require(
        isinstance(value, dict)
        and set(value)
        == {"version", "observed_at", "expires_at", "policy_snapshot", "offers"}
        and value["version"] == 1
        and type(value["version"]) is int
        and finite(value["observed_at"], minimum=1)
        and finite(value["expires_at"], minimum=value["observed_at"])
        and 0 < value["expires_at"] - value["observed_at"] <= FRESH_SECONDS
        and isinstance(value["policy_snapshot"], str)
        and re.fullmatch(r"[0-9a-f]{64}", value["policy_snapshot"])
        and isinstance(value["offers"], list)
        and 0 < len(value["offers"]) <= MAX_OFFERS,
        "host_selection_invalid",
    )
    seen = set()
    for item in value["offers"]:
        require(
            isinstance(item, dict)
            and {"offer_id", "machine_id"}
            <= set(item)
            <= {"offer_id", "machine_id"} | OFFER_METADATA_FIELDS,
            "host_selection_invalid",
        )
        offer_metadata({key: item[key] for key in OFFER_METADATA_FIELDS if key in item})
        offer, _ = numeric_id(item["offer_id"]), numeric_id(item["machine_id"])
        require(offer not in seen, "host_offer_identity_conflict")
        seen.add(offer)
    if now is not None:
        require(
            finite(now, minimum=value["observed_at"], maximum=value["expires_at"])
            and now < value["expires_at"],
            "host_selection_expired",
        )
    return copy.deepcopy(value)
