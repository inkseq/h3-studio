"""Typed host evidence and policy on the original operator records.

No provider effects or new accounting/telemetry store. Only positively bound
physical identity participates in automatic policy; legacy unknowns stay unknown.
"""

from __future__ import annotations

import copy
import itertools
import math
import re

import uuid

from sqlalchemy import insert, select, update

from .operator_capacity import (
    operator_commands,
    operator_nodes,
    operator_policy,
    require,
)
from .providers.registry import LIUM, VASTAI
from .repository import outbox, request_hash

MAX_NODES = 4096
MAX_OFFERS = 1000
FRESH_SECONDS = 120
POLICIES = {"default", "prefer", "deny"}
REASONS = {
    "default": "operator_host_default",
    "prefer": "operator_host_preferred",
    "deny": "operator_host_denied",
}
# An admin may also deny a host for what it produced, e.g. corrupted takes
# (#900: a Lium host on staging, 2026-11-09).
BAD_OUTPUT = "operator_host_bad_output"
ADMIN_REASONS = {*REASONS.items(), ("deny", BAD_OUTPUT)}
# A measured slow download denies the host until an admin restores it (owner,
# 2026-10-31). Stored as the manual policy so the existing admin write clears it.
AUTOMATIC_SLOW_DOWNLOAD = "automatic_slow_download"
# A denied host is never rented again on its own; only an admin clears it
# (owner, 2026-11-10). One rental that never started is enough; a host that
# started and then failed is denied on its second failure within a day.
AUTOMATIC_START_FAILURE = "automatic_start_failure"
AUTOMATIC_REPEATED_FAILURE = "automatic_repeated_failure"
# These say nothing about the supplier's other machines; a measured slow
# download does, because machines of one supplier share its network.
MACHINE_ONLY_REASONS = {AUTOMATIC_START_FAILURE, AUTOMATIC_REPEATED_FAILURE}
STORED_REASONS = {
    *ADMIN_REASONS,
    ("deny", AUTOMATIC_SLOW_DOWNLOAD),
    ("deny", AUTOMATIC_START_FAILURE),
    ("deny", AUTOMATIC_REPEATED_FAILURE),
}
# Providers whose physical host is positively bound, and how (#900). A Vast
# machine id or a Lium executor id names the host; an instance or pod does not.
# RunPod and CloudRift place a pod on a host the renter cannot choose, so a
# deny there could not exclude anything before the machine is paid for.
HOST_SOURCES = {VASTAI.id: "vast_machine_id", LIUM.id: "lium_executor_id"}
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
DOWNLOAD_STOPS = {"dstack_download_timebox_exceeded", "dstack_download_too_slow"}
START_FAILURES = {
    "provider_provisioning_timeout",
    "provider_image_pull_failed",
    "provider_runner_timeout",
}
# dstack termination reason -> (stage, reason) of a start the host failed:
# the instance never became reachable, or its runner never answered (#1198).
DSTACK_START_FAILURES = {
    "waiting_instance_limit_exceeded": (
        "provisioning",
        "provider_provisioning_timeout",
    ),
    "waiting_runner_limit_exceeded": ("runtime_start", "provider_runner_timeout"),
}
START_FAILURE_RETRY_LAST_SECONDS = 3600
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


# An upstream (CDN or region) slowdown makes every host look slow. Once this
# many distinct hosts are slow-stopped within the window, later slow hosts are
# still stopped and replaced, but no longer denied, and one alert is raised
# per window (#903).
UPSTREAM_BRAKE_HOSTS = 3
UPSTREAM_BRAKE_SECONDS = 3600
SLOW_STOP_EVENT = "dstack.download_slow_stop"
UPSTREAM_ALERT = "capacity_download_upstream_slow"
START_FAILURE_EVENT = "dstack.start_failure_stop"
START_FAILURES_ALERT = "capacity_start_failures_upstream"
GENERATE_FAILURE_EVENT = "dstack.generate_failure_stop"
GENERATE_FAILURES_ALERT = "capacity_generate_failures_upstream"


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


def host_provider(value):
    require(value in HOST_SOURCES, "host_provider_unsupported", 422)
    return value


def host_id(provider, value):
    """A provider's physical host id: a Vast machine id or a Lium executor id."""
    if host_provider(provider) == VASTAI.id:
        return numeric_id(value)
    require(
        isinstance(value, str) and _UUID.fullmatch(value),
        "host_identity_invalid",
        422,
    )
    return value


def host_sort_key(value):
    """Vast machine ids in numeric order, then Lium executors."""
    provider, machine = value
    return (
        provider != VASTAI.id,
        int(machine) if provider == VASTAI.id else 0,
        machine,
    )


def instance_id(provider, value):
    """The rented instance (Vast) or pod (Lium) on that host."""
    if provider == VASTAI.id:
        return numeric_id(value)
    require(
        isinstance(value, str)
        and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}", value),
        "host_observation_invalid",
    )
    return value


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
        and value["provider"] in HOST_SOURCES
        and value["identity_confirmed"] is True
        and value["physical_id_source"] == HOST_SOURCES[value["provider"]],
        "host_observation_invalid",
    )
    provider = value["provider"]
    host_id(provider, value["machine_id"])
    instance_id(provider, value["provider_instance_id"])
    if provider == VASTAI.id:
        numeric_id(value["offer_id"])
    else:
        # A Lium offer is the executor itself.
        require(value["offer_id"] == value["machine_id"], "host_observation_invalid")
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
            and bound.get("backend") == value["provider"]
            and bound.get("provider_instance_id") == value["provider_instance_id"],
            "host_observation_binding_invalid",
        )
        return value
    except (ValueError, KeyError):
        return None


def lium_observation(provisioning, binding, now):
    """Bind a Lium pod to its executor from the dstack allocation (#900).

    dstack's Lium backend rents one executor per offer and records it as the
    allocation's instance type name, so that name is the physical host. No
    extra provider read is made. None until the pod has a usable identity;
    an executor outside a frozen selector refuses the allocation.
    """
    kind = provisioning.get("instance_type")
    resource = kind.get("resources", {}) if isinstance(kind, dict) else {}
    gpus = resource.get("gpus") if isinstance(resource, dict) else None
    price = provisioning.get("price")
    instance = provisioning.get("instance_id")
    executor = kind.get("name") if isinstance(kind, dict) else None
    if (
        provisioning.get("backend") != LIUM.id
        or not isinstance(executor, str)
        or not _UUID.fullmatch(executor)
        or not isinstance(instance, str)
        or binding.get("provider_instance_id") not in (None, instance)
        or not isinstance(gpus, list)
        or len(gpus) != 1
        or not isinstance(gpus[0], dict)
        or not finite(price)
        or price <= 0
    ):
        return None
    selected = binding.get("host_selection")
    if selected is not None:
        approved = {row["machine_id"] for row in selection(selected)["offers"]}
        require(executor in approved, "dstack_host_allocation_changed")
    value = {
        "version": 1,
        "provider": LIUM.id,
        "machine_id": executor,
        "provider_instance_id": instance,
        "offer_id": executor,
        "observed_at": now,
        "region": provisioning.get("region"),
        "gpu": gpus[0].get("name"),
        "gpu_count": 1,
        "allocated_ram_mib": resource.get("memory_mib"),
        "allocated_vram_mib": gpus[0].get("memory_mib"),
        "cpu": resource.get("cpus"),
        "disk_mib": (resource.get("disk") or {}).get("size_mib"),
        "reliability": None,
        "quoted_hourly_microusd": math.ceil(price * 1_000_000),
        "identity_confirmed": True,
        "physical_id_source": HOST_SOURCES[LIUM.id],
    }
    try:
        return observation(value, now=now)
    except ValueError:
        return None


def slow_stop_braked(connection, tenant, node_id, physical, now):
    """Journal one slow-download stop; True when the upstream brake applies.

    Every slow stop of a bound host is recorded (node, provider, host). The
    brake counts distinct hosts stopped for slowness within the window, this
    one included.
    """
    return _upstream_braked(
        connection,
        tenant,
        node_id,
        physical,
        now,
        event=SLOW_STOP_EVENT,
        alert=UPSTREAM_ALERT,
        diagnosis="download_upstream_slow",
        count="slow_hosts",
    )


def start_failure_braked(connection, tenant, node_id, physical, now):
    """Journal one failed start; True when many hosts fail to start at once.

    Several fresh hosts failing the same way within the hour point at our own
    image or registry, not at the hosts: stop denying and raise one alert.
    """
    return _upstream_braked(
        connection,
        tenant,
        node_id,
        physical,
        now,
        event=START_FAILURE_EVENT,
        alert=START_FAILURES_ALERT,
        diagnosis="start_failures_upstream",
        count="failed_hosts",
    )


def generate_failure_braked(connection, tenant, node_id, physical, now):
    """Journal one failure after ready; True when many hosts fail that way at once.

    Three distinct hosts going silent or over time within the hour is taken
    to be ours (a release, a request), not three bad hosts (#1214): stop
    denying and raise one alert.
    """
    return _upstream_braked(
        connection,
        tenant,
        node_id,
        physical,
        now,
        event=GENERATE_FAILURE_EVENT,
        alert=GENERATE_FAILURES_ALERT,
        diagnosis="generate_failures_upstream",
        count="failed_hosts",
    )


def _upstream_braked(
    connection, tenant, node_id, physical, now, *, event, alert, diagnosis, count
):
    connection.execute(
        insert(outbox).values(
            id=str(uuid.uuid4()),
            event_type=event,
            aggregate_id=node_id,
            payload={
                "tenant_id": tenant,
                "provider": physical["provider"],
                "machine_id": physical["machine_id"],
            },
            created_at=now,
        )
    )
    rows = connection.execute(
        select(outbox.c.payload).where(
            outbox.c.event_type == event,
            outbox.c.created_at >= now - UPSTREAM_BRAKE_SECONDS,
            outbox.c.payload["tenant_id"].as_string() == tenant,
        )
    ).scalars()
    hosts = {
        (row.get("provider"), row.get("machine_id"))
        for row in rows
        if isinstance(row, dict)
    }
    if len(hosts) < UPSTREAM_BRAKE_HOSTS:
        return False
    alerted = connection.execute(
        select(outbox.c.id)
        .where(
            outbox.c.event_type == "capacity.health_alert",
            outbox.c.created_at >= now - UPSTREAM_BRAKE_SECONDS,
            outbox.c.payload["tenant_id"].as_string() == tenant,
            outbox.c.payload["code"].as_string() == alert,
        )
        .limit(1)
    ).first()
    if alerted is None:
        identity = str(uuid.uuid4())
        connection.execute(
            insert(outbox).values(
                id=identity,
                event_type="capacity.health_alert",
                aggregate_id=node_id,
                payload={
                    "version": 1,
                    "code": alert,
                    "diagnosis": diagnosis,
                    "resource_id": node_id,
                    "since": now,
                    count: len(hosts),
                    "event_id": identity,
                    "tenant_id": tenant,
                },
                created_at=now,
            )
        )
    return True


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
            and value["reason_code"] in DOWNLOAD_STOPS
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


def denied_suppliers(store, connection, nodes, offers, *, now=None):
    """Suppliers with a denied machine, from today's offers and the offers frozen at past starts."""
    machines = {}
    for node in nodes:
        frozen = node.get("payload", {}).get("dstack", {}).get("host_selection")
        for item in frozen.get("offers", []) if isinstance(frozen, dict) else []:
            if isinstance(item, dict) and item.get("host_id"):
                machines[item["machine_id"]] = item["host_id"]
    for item in offers:
        if item.get("host_id"):
            machines[item["machine_id"]] = item["host_id"]
    denied = set()
    for machine, supplier in machines.items():
        policy = store.read(connection, machine, nodes=nodes, now=now)[0]
        if (
            policy["effective"] == "deny"
            and policy["reason_code"] not in MACHINE_ONLY_REASONS
        ):
            denied.add(supplier)
    return denied


def _rental_outcome(node, *, now):
    """(host, provider, latest failure (at, start stage) or None, ready at or None)."""
    physical = bound_observation(node, now=now)
    if not physical:
        return None
    payload = node.get("payload", {})
    found = []
    try:
        item = failure(payload.get("host_failure"), now=now)
        if item["qualified_start_failure"]:
            found.append((item["at"], True))
    except (ValueError, KeyError):
        pass
    recovered = capacity_failure(node, now=now)
    if recovered is not None:
        found.append((recovered["at"], recovered["stage"] == "runtime_start"))
    ready = payload.get("dstack", {}).get("first_runtime_ready_at")
    return (
        physical["machine_id"],
        physical["provider"],
        # Several observations/reasons from one original rental count once.
        max(found) if found else None,
        ready if finite(ready, minimum=1, maximum=now) else None,
    )


def automatic_deny(nodes, machine_id, *, now, provider=VASTAI.id):
    """Derive fixed seven-day intervals from distinct original failed rentals.

    A stopped-by-owner row, a model-load problem, missing identity, or repeated
    observations of one rental can never count as a second startup failure.
    Two start failures with a good start of the same host between them are not
    a pair: a slow image pull can time out once on a host that works (#1198).
    """
    events, readies = {}, []
    for node in nodes:
        outcome = _rental_outcome(node, now=now)
        if outcome is None or outcome[:2] != (machine_id, provider):
            continue
        if outcome[2] is not None and outcome[2][0] >= now - 8 * 86400:
            events[node["intent_id"]] = outcome[2]
        if outcome[3] is not None:
            readies.append(outcome[3])
    events = sorted(events.values())
    expires = None
    for (before, before_start), (at, start) in itertools.pairwise(events):
        if at - before > 86400 or (
            start and before_start and any(before < ready < at for ready in readies)
        ):
            continue
        until = at + 7 * 86400
        if now < until:
            expires = max(expires or 0, until)
    return expires


def recent_start_failures(nodes, *, now, provider=VASTAI.id):
    """Hosts whose last rental failed to start within the last hour (#1198).

    The next start tries another host first. This is an order, not a deny: the
    only host on offer is still rented again, and often works the second time.
    """
    failed, ready = {}, {}
    for node in nodes:
        outcome = _rental_outcome(node, now=now)
        if outcome is None or outcome[1] != provider:
            continue
        if outcome[2] is not None and outcome[2][1]:
            failed[outcome[0]] = max(failed.get(outcome[0], 0), outcome[2][0])
        if outcome[3] is not None:
            ready[outcome[0]] = max(ready.get(outcome[0], 0), outcome[3])
    return {
        host
        for host, at in failed.items()
        if at >= now - START_FAILURE_RETRY_LAST_SECONDS and at > ready.get(host, 0)
    }


def recent_silent_failures(nodes, *, now, provider=VASTAI.id):
    """Hosts whose last rental failed after ready within the last hour (#1214).

    The retry of that work tries another host first: on the same host again,
    a bad host and a bad request look the same. An order, not a deny; the
    only host on offer is still rented again.
    """
    failed, ready = {}, {}
    for node in nodes:
        outcome = _rental_outcome(node, now=now)
        if outcome is None or outcome[1] != provider:
            continue
        if outcome[2] is not None and not outcome[2][1]:
            failed[outcome[0]] = max(failed.get(outcome[0], 0), outcome[2][0])
        if outcome[3] is not None:
            ready[outcome[0]] = max(ready.get(outcome[0], 0), outcome[3])
    return {
        host
        for host, at in failed.items()
        if at >= now - START_FAILURE_RETRY_LAST_SECONDS and at > ready.get(host, 0)
    }


def deny_failed_rental(repo, tenant, connection, node, *, now):
    """Deny the bound host of a rental that just failed; True when denied.

    The caller holds the capacity lock and passes the node as it will be
    stored. A rental that never started denies its host at once, unless many
    hosts fail to start within the hour (start_failure_braked). A rental that
    started and failed later denies its host on the second such failure
    within a day, unless many hosts fail that way within the hour
    (generate_failure_braked). Either deny stays until an admin clears it.
    """
    outcome = _rental_outcome(node, now=now)
    if outcome is None or outcome[2] is None:
        return False
    machine_id, provider, (_, start), _ = outcome
    if start:
        physical = bound_observation(node, now=now)
        if start_failure_braked(connection, tenant, node["intent_id"], physical, now):
            return False
        reason = AUTOMATIC_START_FAILURE
    else:
        physical = bound_observation(node, now=now)
        if generate_failure_braked(
            connection, tenant, node["intent_id"], physical, now
        ):
            return False
        nodes = [
            row
            for row in original_nodes(connection, tenant)
            if row["intent_id"] != node["intent_id"]
        ]
        if (
            automatic_deny([*nodes, node], machine_id, now=now, provider=provider)
            is None
        ):
            return False
        reason = AUTOMATIC_REPEATED_FAILURE
    return HostPolicyStore(repo, tenant).deny_failed_host(
        connection,
        machine_id,
        node_id=node["intent_id"],
        now=now,
        provider=provider,
        reason_code=reason,
    )


def capacity_failure(node, *, now):
    """Repeated owned timeouts count as reliability evidence, not host diagnosis."""
    value = node.get("payload", {}).get("capacity_watchdog")
    if (
        not isinstance(value, dict)
        or value.get("version") != 1
        or node.get("desired_state") != "stopped"
        or value.get("reason")
        not in {
            "capacity_no_progress",
            "capacity_runtime_exceeded",
            "capacity_startup_timeout",
            "capacity_startup_failed",
        }
        or not finite(value.get("at"), minimum=1, maximum=now)
        or bound_observation(node, now=now) is None
    ):
        return None
    return {
        "stage": "runtime_start"
        if value["reason"] in {"capacity_startup_timeout", "capacity_startup_failed"}
        else "generate",
        "reason_code": value["reason"],
        "at": value["at"],
        "outcome": "failure",
    }


class HostPolicyStore:
    def __init__(self, repo, tenant):
        self.repo, self.tenant = repo, tenant

    def key(self, machine_id, provider=VASTAI.id):
        host_id(provider, machine_id)
        # The original shared operator_policy.id is VARCHAR(30). Keyed on
        # provider plus host id; a Vast key is unchanged from before #900.
        return (
            "host:"
            + request_hash(
                {"tenant": self.tenant, "provider": provider, "machine_id": machine_id}
            )[:25]
        )

    def read(self, connection, machine_id, *, nodes=None, now=None, provider=VASTAI.id):
        host_id(provider, machine_id)
        now = self.repo.clock() if now is None else now
        row = (
            connection.execute(
                select(operator_policy).where(
                    operator_policy.c.id == self.key(machine_id, provider)
                )
            )
            .mappings()
            .first()
        )
        value = {
            "version": 1,
            "tenant_id": self.tenant,
            "provider": provider,
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
                and (stored["manual"], stored["reason_code"]) in STORED_REASONS
                and type(row["version"]) is int
                and row["version"] >= 1,
                "host_policy_binding_invalid",
            )
            value, revision = copy.deepcopy(stored), row["version"]
        nodes = original_nodes(connection, self.tenant) if nodes is None else nodes
        until = automatic_deny(nodes, machine_id, now=now, provider=provider)
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

    def deny_slow_download(
        self, connection, machine_id, *, node_id, now, provider=VASTAI.id
    ):
        """Persistently deny one host after a measured slow download."""
        return self._deny_automatic(
            connection,
            machine_id,
            node_id=node_id,
            now=now,
            provider=provider,
            reason_code=AUTOMATIC_SLOW_DOWNLOAD,
            actor="system:download-speed-gate",
            key="slow-download",
        )

    def deny_failed_host(
        self, connection, machine_id, *, node_id, now, provider, reason_code
    ):
        """Persistently deny one host whose rental failed (never retried)."""
        require(reason_code in MACHINE_ONLY_REASONS, "host_policy_reason_invalid")
        return self._deny_automatic(
            connection,
            machine_id,
            node_id=node_id,
            now=now,
            provider=provider,
            reason_code=reason_code,
            actor="system:host-failure",
            key="host-failure",
        )

    def _deny_automatic(
        self, connection, machine_id, *, node_id, now, provider, reason_code, actor, key
    ):
        """Store a deny that only an admin clears.

        The caller holds the capacity lock. An existing manual deny or prefer
        is kept; a default policy is replaced and audited like an admin change.
        """
        old, value = self.read(connection, machine_id, now=now, provider=provider)
        # A host an admin chose to prefer is stopped but never auto-denied (#903).
        if old["manual"] in {"deny", "prefer"}:
            return False
        value.update(manual="deny", reason_code=reason_code)
        row = {
            "version": old["revision"] + 1,
            "payload": value,
            "actor": actor,  # Not an admin principal.
            "updated_at": now,
        }
        if old["revision"]:
            connection.execute(
                update(operator_policy)
                .where(operator_policy.c.id == self.key(machine_id, provider))
                .values(**row)
            )
        else:
            connection.execute(
                insert(operator_policy).values(id=self.key(machine_id, provider), **row)
            )
        new = self.read(connection, machine_id, now=now, provider=provider)[0]
        command_id = str(uuid.uuid4())
        receipt = {
            "state": "completed",
            "changed": True,
            "host": {"provider": provider, "machine_id": machine_id},
            "policy": new,
            "command_id": command_id,
            "node_id": node_id,
        }
        connection.execute(
            insert(operator_commands).values(
                id=command_id,
                actor=actor,
                # One receipt per policy revision: a node denied again after an
                # admin restore must not collide with its earlier receipt.
                idempotency_key=f"{key}-{node_id}-{old['revision']}",
                request_hash=request_hash(
                    {
                        "kind": "host_policy",
                        "node": node_id,
                        "revision": old["revision"],
                    }
                ),
                kind="host_policy",
                state="completed",
                payload={"result": receipt},
                reason_code=reason_code,
                created_at=now,
                updated_at=now,
            )
        )
        fields = ("manual", "revision", "snapshot")
        connection.execute(
            insert(outbox).values(
                id=str(uuid.uuid4()),
                event_type="operator.host_policy_updated",
                aggregate_id=machine_id,
                payload={
                    "tenant_id": self.tenant,
                    "provider": provider,
                    "machine_id": machine_id,
                    "actor_id": actor,
                    "actor_owner": None,
                    "reason_code": reason_code,
                    "old": {k: old[k] for k in fields},
                    "new": {k: new[k] for k in fields},
                    "command_id": command_id,
                    "node_id": node_id,
                },
                created_at=now,
            )
        )
        return True

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
            (row["provider"], host_id(row["provider"], row["machine_id"]))
            for row in rows
            if isinstance(row, dict) and row.get("provider") in HOST_SOURCES
        }

    def admission_snapshot(self, connection, offers, *, now=None, provider=VASTAI.id):
        require(
            isinstance(offers, (list, tuple)) and 0 < len(offers) <= MAX_OFFERS,
            "host_selection_invalid",
        )
        nodes = original_nodes(connection, self.tenant)
        policies = {}
        for item in offers:
            require(isinstance(item, dict), "host_selection_invalid")
            machine = host_id(provider, item["machine_id"])
            if machine not in policies:
                policies[machine] = self.read(
                    connection, machine, nodes=nodes, now=now, provider=provider
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


def selection_provider(value):
    """A frozen selector's provider; selectors before #900 are Vast."""
    return value.get("provider", VASTAI.id) if isinstance(value, dict) else None


def selection(value, *, now=None):
    fields = {"version", "observed_at", "expires_at", "policy_snapshot", "offers"}
    require(
        isinstance(value, dict)
        and set(value) - {"provider"} == fields
        and selection_provider(value) in HOST_SOURCES
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
            <= {"offer_id", "machine_id", "host_id"} | OFFER_METADATA_FIELDS,
            "host_selection_invalid",
        )
        offer_metadata({key: item[key] for key in OFFER_METADATA_FIELDS if key in item})
        provider = selection_provider(value)
        machine = host_id(provider, item["machine_id"])
        if provider == VASTAI.id:
            offer = numeric_id(item["offer_id"])
        else:
            offer = item["offer_id"]
            require(offer == machine, "host_selection_invalid")
        require(offer not in seen, "host_offer_identity_conflict")
        seen.add(offer)
    if now is not None:
        require(
            finite(now, minimum=value["observed_at"], maximum=value["expires_at"])
            and now < value["expires_at"],
            "host_selection_expired",
        )
    return copy.deepcopy(value)
