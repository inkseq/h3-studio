"""Bounded original-ledger health alerts and the existing demand safety pause.

The controller owns the existing cadence. This projection never grants a job,
changes a budget, invents provider absence, or treats a CPU lease as progress.
"""

from __future__ import annotations

import copy
import math
import uuid

from sqlalchemy import insert, select, update

from .dstack_demand import _matches
from .operator_capacity import operator_nodes, operator_policy
from .repository import (
    attempts,
    instance_intents,
    jobs,
    outbox,
    registered_workers,
    request_hash,
)

TERMINAL = {"succeeded", "failed", "cancelled"}
QUEUED = {"queued", "waiting_capacity"}
HOST_FAULTS = {"runtime_process_exited", "runtime_readiness_timeout", "CUDAUnavailable"}


def finite(value, now):
    return type(value) in (int, float) and math.isfinite(value) and 0 <= value <= now


def finding(code, resource, since, diagnosis, *, node_ids=(), stop_replace=False):
    return {
        "version": 1,
        "code": code,
        "resource_id": resource,
        "since": since,
        "diagnosis": diagnosis,
        "node_ids": list(node_ids),
        "stop_replace": stop_replace,
        "episode": request_hash(
            {"code": code, "resource_id": resource, "since": since}
        ),
    }


def matching(node, job):
    try:
        return _matches(node, job)
    except (KeyError, TypeError, AttributeError):
        return False


def active_attempt(job, attempt, now):
    """A retained deferred attempt is history, not an engine or worker lease."""
    if (
        job["current_attempt_id"] != attempt["id"]
        or job["status"] in TERMINAL
        or attempt["status"] == "attempt_lost"
    ):
        return False
    submitted = finite(attempt.get("submission_started_at"), now) or bool(
        isinstance(attempt.get("upstream_task_id"), str) and attempt["upstream_task_id"]
    )
    expires = job.get("lease_expires_at")
    leased = (
        job.get("lease_worker_id") == attempt["worker_id"]
        and type(expires) in (int, float)
        and math.isfinite(expires)
        and expires > now
    )
    return submitted or leased


def evaluate(
    nodes,
    job_rows,
    attempt_rows,
    workers,
    *,
    now,
    queue_ages=None,
    demand_enabled=False,
    demand_enabled_since=None,
    paid_idle_seconds=600,
    queue_age_seconds=1800,
    loop_window_seconds=3600,
):
    """Closed findings from one original snapshot; updated_at is not a clock."""
    if not finite(now, now) or any(
        type(value) is not int or value < 1
        for value in (paid_idle_seconds, queue_age_seconds, loop_window_seconds)
    ):
        raise ValueError("capacity_health_context_invalid")
    if max(len(nodes), len(job_rows), len(attempt_rows), len(workers)) > 10000:
        raise ValueError("capacity_health_snapshot_too_large")
    queue_ages = queue_ages or {}
    jobs = {job["id"]: job for job in job_rows}
    queued = [job for job in job_rows if job["status"] in QUEUED]
    result = []
    for job in queued:
        since = queue_ages.get(job["id"], job.get("created_at"))
        if finite(since, now) and now - since > queue_age_seconds:
            result.append(finding("capacity_queue_age", job["id"], since, "queue_age"))
    empty_cycles = []
    for node in nodes:
        payload = node["payload"]
        binding = payload.get("dstack", {})
        instance = node.get("provider_instance_id", binding.get("provider_instance_id"))
        associated = [
            worker
            for worker in workers
            if (
                worker["id"] == "dstack-" + node["intent_id"].replace("-", "")
                or isinstance(instance, str)
                and bool(instance)
                and worker.get("instance_id") == instance
                and worker.get("provider") == node.get("provider")
            )
        ]
        worker_ids = {
            "dstack-" + node["intent_id"].replace("-", ""),
            *[worker["id"] for worker in associated],
        }
        related = [
            attempt for attempt in attempt_rows if attempt["worker_id"] in worker_ids
        ]
        served = [
            attempt
            for attempt in related
            if finite(attempt.get("submission_started_at"), now)
        ]
        started = binding.get("apply_started_at")
        stopped = binding.get("first_stopped_at")
        if (
            demand_enabled
            and node["runtime_state"] == "stopped"
            and finite(started, now)
            and finite(stopped, now)
            and now - loop_window_seconds <= started <= stopped
            and (
                not finite(demand_enabled_since, now) or stopped >= demand_enabled_since
            )
            and not served
        ):
            empty_cycles.append((stopped, node["intent_id"]))
        if (
            node["desired_state"] != "running"
            or binding.get("apply_started") is not True
        ):
            continue
        capable = binding.get("first_runtime_ready_at")
        if not finite(capable, now):
            capable = (
                payload.get("capacity_health", {}).get("registered_observed_at")
                if associated
                else None
            )
        matching_queue = [job for job in queued if matching(node, job)]
        if not finite(capable, now) or not matching_queue:
            continue
        if any(
            (job := jobs.get(attempt["job_id"])) is not None
            and active_attempt(job, attempt, now)
            for attempt in related
        ):
            continue
        activity = [capable, *[attempt["submission_started_at"] for attempt in served]]
        # A closed original attempt timestamp is meaningful; live updated_at,
        # worker expiry and repeated unknown-poll timestamps are excluded.
        activity.extend(
            attempt["updated_at"]
            for attempt in related
            if attempt["status"] in TERMINAL and finite(attempt.get("updated_at"), now)
        )
        queued_since = [
            queue_ages.get(job["id"], job.get("created_at")) for job in matching_queue
        ]
        queued_since = [stamp for stamp in queued_since if finite(stamp, now)]
        if not queued_since:
            continue
        since = max(max(activity), min(queued_since))
        if now - since <= paid_idle_seconds:
            continue
        registration = binding.get("registration_observation", {})
        code = binding.get("reason_code")
        observed = binding.get("observed_at")
        host_fault = (
            code in HOST_FAULTS and finite(observed, now) and now - observed <= 120
        )
        diagnosis = (
            "registration_capacity"
            if registration.get("reason_code") == "global_capacity_exceeded"
            else "host_runtime"
            if host_fault
            else "dispatch_unobserved"
        )
        result.append(
            finding(
                "capacity_paid_idle_queue",
                node["intent_id"],
                since,
                diagnosis,
                node_ids=(node["intent_id"],),
                stop_replace=host_fault and diagnosis == "host_runtime",
            )
        )
    empty_cycles = sorted(
        (at, identity)
        for identity, at in {
            identity: at for at, identity in sorted(set(empty_cycles))
        }.items()
    )
    if len(empty_cycles) >= 2:
        last = empty_cycles[-2:]
        result.append(
            finding(
                "capacity_demand_start_loop",
                last[-1][1],
                last[0][0],
                "unserved_start_stop_loop",
                node_ids=[node for _, node in last],
            )
        )
    return result


def snapshot(service, connection, now):
    from .alert_snapshot import _state_age
    from .dstack_demand import _nodes
    from .dstack_demand_policy import policy_key, read_runtime

    tenant = service.settings.tenant_id
    nodes = _nodes(connection, tenant)
    providers = {
        row.id: row.provider
        for row in connection.execute(
            select(instance_intents.c.id, instance_intents.c.provider)
        )
    }
    for node in nodes:
        node["provider"] = providers[node["intent_id"]]
    job_rows = [
        dict(row)
        for row in connection.execute(
            select(jobs).where(jobs.c.tenant_id == tenant).limit(10001)
        ).mappings()
    ]
    attempt_rows = [
        dict(row)
        for row in connection.execute(
            select(attempts)
            .join(jobs, attempts.c.job_id == jobs.c.id)
            .where(jobs.c.tenant_id == tenant)
            .limit(10001)
        ).mappings()
    ]
    workers = [
        dict(row)
        for row in connection.execute(
            select(registered_workers).limit(10001)
        ).mappings()
    ]
    policy = (
        connection.execute(
            select(operator_policy).where(operator_policy.c.id == policy_key(tenant))
        )
        .mappings()
        .first()
    )
    enabled = read_runtime(connection, tenant)["enabled"]
    findings = evaluate(
        nodes,
        job_rows,
        attempt_rows,
        workers,
        now=now,
        queue_ages=_state_age(connection, tenant),
        demand_enabled=enabled,
        demand_enabled_since=policy["updated_at"] if policy else None,
    )
    return findings, policy


def observe_registered(service):
    """Persist only the first positive registration observation, never a lease."""
    from .dstack_demand import _nodes

    repo = service.repo
    with repo.engine.connect() as connection:
        nodes = _nodes(connection, service.settings.tenant_id)
        workers = list(connection.execute(select(registered_workers)).mappings())
    for node in nodes:
        binding = node["payload"].get("dstack", {})
        if (
            node["desired_state"] != "running"
            or finite(binding.get("first_runtime_ready_at"), repo.clock())
            or finite(
                node["payload"]
                .get("capacity_health", {})
                .get("registered_observed_at"),
                repo.clock(),
            )
        ):
            continue
        identity = "dstack-" + node["intent_id"].replace("-", "")
        if not any(
            worker["id"] == identity and worker["state"] != "retired"
            for worker in workers
        ):
            continue
        with repo.transaction() as connection:
            repo._lock_capacity(connection)
            current = repo._locked(
                connection,
                select(operator_nodes).where(
                    operator_nodes.c.intent_id == node["intent_id"]
                ),
            )
            if current["desired_state"] != "running":
                continue
            payload = copy.deepcopy(current["payload"])
            health = payload.setdefault("capacity_health", {})
            if "registered_observed_at" not in health:
                health["registered_observed_at"] = repo.clock()
                connection.execute(
                    update(operator_nodes)
                    .where(operator_nodes.c.intent_id == node["intent_id"])
                    .values(payload=payload)
                )


def _event(connection, tenant, value, now):
    identity = str(uuid.uuid4())
    payload = {**value, "event_id": identity, "tenant_id": tenant}
    connection.execute(
        insert(outbox).values(
            id=identity,
            event_type="capacity.health_alert",
            aggregate_id=value["resource_id"],
            payload=payload,
            created_at=now,
        )
    )
    return payload


def record(service, value, *, policy_version=None):
    """Deduplicate on the original resource; pause only the existing switch."""
    from .dstack_demand_policy import policy_key, read_runtime

    repo, tenant = service.repo, service.settings.tenant_id
    now = repo.clock()
    code = value["code"]
    with repo.transaction() as connection:
        if code == "capacity_demand_start_loop":
            repo._lock_capacity(connection)
            row = repo._locked(
                connection,
                select(operator_policy).where(
                    operator_policy.c.id == policy_key(tenant)
                ),
            )
            if (
                row is None
                or row["version"] != policy_version
                or not read_runtime(connection, tenant)["enabled"]
            ):
                return None
            # The closed policy shape stays unchanged. This is a controller
            # safety action, never impersonation of a human administrator.
            connection.execute(
                update(operator_policy)
                .where(operator_policy.c.id == row["id"])
                .values(
                    payload={"tenant_id": tenant, "enabled": False},
                    version=row["version"] + 1,
                    actor="dstack-health",
                    updated_at=now,
                )
            )
            payload = _event(connection, tenant, value, now)
            repo._emit(
                connection,
                "dstack.demand_safety_paused",
                row["id"],
                {
                    "reason_code": code,
                    "event_id": payload["event_id"],
                    "node_ids": value["node_ids"],
                },
            )
            return payload
        table, key = (
            (jobs, jobs.c.id)
            if code == "capacity_queue_age"
            else (operator_nodes, operator_nodes.c.intent_id)
        )
        if table is operator_nodes:
            repo._lock_capacity(connection)
        row = repo._locked(connection, select(table).where(key == value["resource_id"]))
        if row is None:
            return None
        if table is jobs:
            if row["tenant_id"] != tenant or row["status"] not in QUEUED:
                return None
            previous = connection.execute(
                select(outbox.c.id)
                .where(
                    outbox.c.event_type == "capacity.health_alert",
                    outbox.c.aggregate_id == row["id"],
                    outbox.c.payload["episode"].as_string() == value["episode"],
                )
                .limit(1)
            ).first()
            return None if previous else _event(connection, tenant, value, now)
        else:
            if (
                row["payload"].get("tenant_id") != tenant
                or row["desired_state"] != "running"
            ):
                return None
            container = copy.deepcopy(row["payload"])
        health = container.setdefault("capacity_health", {})
        previous = health.get(code)
        if isinstance(previous, dict) and previous.get("episode") == value["episode"]:
            return None
        payload = _event(connection, tenant, value, now)
        health[code] = {
            "episode": value["episode"],
            "event_id": payload["event_id"],
            "since": value["since"],
            "diagnosis": value["diagnosis"],
        }
        connection.execute(
            update(table).where(key == value["resource_id"]).values(payload=container)
        )
        return payload


def pending_reports(service, telemetry, reporter=None):
    """Retry immutable outbox IDs; successful acknowledgement is another event."""
    repo, tenant = service.repo, service.settings.tenant_id
    acknowledged = outbox.alias("acknowledged_health")
    acknowledged_event = (
        select(acknowledged.c.id)
        .where(
            acknowledged.c.event_type == "capacity.health_alert_reported",
            acknowledged.c.payload["alert_event_id"].as_string() == outbox.c.id,
        )
        .exists()
    )
    with repo.engine.connect() as connection:
        pending = [
            dict(row["payload"])
            for row in connection.execute(
                select(outbox)
                .where(
                    outbox.c.event_type == "capacity.health_alert",
                    outbox.c.payload["tenant_id"].as_string() == tenant,
                    ~acknowledged_event,
                )
                .order_by(outbox.c.created_at, outbox.c.id)
                .limit(100)
            ).mappings()
        ]
    accepted = []
    for value in pending:
        grafana_ok = True
        try:
            if telemetry.enabled:
                context = {
                    "job_id"
                    if value["code"] == "capacity_queue_age"
                    else "node_id": value["resource_id"]
                }
                grafana_ok = (
                    telemetry.start("queue_wait", context).finish(
                        "failure", error_code=value["code"]
                    )
                    is not None
                )
        except Exception:
            grafana_ok = False
        try:
            sentry_ok = reporter is None or reporter(value) is not None
        except Exception:
            sentry_ok = False
        if grafana_ok and sentry_ok:
            accepted.append(value)
    # One bounded flush for the whole batch, rather than up to 100 sequential
    # one-second waits delaying dispatch and timeout reclamation.
    if telemetry.enabled and pending:
        try:
            if telemetry.force_flush(1000) is False:
                return
        except Exception:
            return
    if accepted:
        with repo.transaction() as connection:
            for value in accepted:
                identity = str(
                    uuid.uuid5(
                        uuid.NAMESPACE_URL, "inkseq-health-ack:" + value["event_id"]
                    )
                )
                if not connection.execute(
                    select(outbox.c.id).where(outbox.c.id == identity)
                ).first():
                    connection.execute(
                        insert(outbox).values(
                            id=identity,
                            event_type="capacity.health_alert_reported",
                            aggregate_id=value["resource_id"],
                            payload={
                                "version": 1,
                                "tenant_id": tenant,
                                "alert_event_id": value["event_id"],
                            },
                            created_at=repo.clock(),
                        )
                    )


def check(service, telemetry, reporter=None):
    """One bounded existing-controller sweep; no provider or task mutations."""
    observe_registered(service)
    with service.repo.engine.connect() as connection:
        values, policy = snapshot(service, connection, service.repo.clock())
    recorded = []
    stops = []
    for value in values:
        event = record(
            service, value, policy_version=policy["version"] if policy else None
        )
        if event:
            recorded.append(event)
        if value["stop_replace"]:
            stops.append(value["resource_id"])
    pending_reports(service, telemetry, reporter)
    return {
        "state": "observed",
        "new_alerts": len(recorded),
        "stop_candidates": stops,
        "demand_paused": any(
            row["code"] == "capacity_demand_start_loop" for row in recorded
        ),
    }
