"""Bounded original-ledger health alerts and the existing demand safety pause.

The controller owns the existing cadence. This projection never grants a job,
changes a budget, invents provider absence, or treats a CPU lease as progress.
"""

from __future__ import annotations

import copy
import math
import uuid

from sqlalchemy import func, insert, select, update

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


# Paid-idle diagnoses that may stop the machine so demand can replace it.
STOP_DIAGNOSES = {"host_runtime", "worker_unreachable"}
# How long a lost worker gets to come back before its machine is stopped (10 min). A deploy
# restarts the app and, with it, the tunnel the worker dials (#1140); chisel retries
# with backoff, so give it a few minutes rather than stopping on the first miss.
WORKER_LOST_GRACE_SECONDS = 600
# A model download still running this long past the floor-rate time is overdue.
DOWNLOAD_OVERDUE_SLACK_SECONDS = 300
# Queued work, no live machine and demand start refusing: alert after 30 min.
START_BLOCKED_SECONDS = 1800
# Job-scoped alerts and the job states in which each is still current.
JOB_ALERTS = {
    "capacity_queue_age": QUEUED,
    "capacity_collect_stuck": {"collecting"},
}
# Alerts whose resource is a job, for the report label; the reclaim writes
# capacity_request_silent itself when it ends a job (capacity_reclaim).
JOB_RESOURCE_ALERTS = frozenset(JOB_ALERTS) | {"capacity_request_silent"}


def download_overdue(binding, now):
    """(started, diagnosis) for a download past its expected time, else None.

    Expected time is the whole model at the owner's floor rate plus slack. The
    controller stops a measured slow or stalled host itself; this catches the
    case where it cannot, for example when readiness reads stop answering.
    """
    from .dstack_preparation import MIN_DOWNLOAD_BYTES_PER_SECOND

    progress = binding.get("download_progress")
    if not isinstance(progress, dict) or progress.get("valid") is not True:
        return None
    started, total = progress.get("started_at"), progress.get("bytes_total")
    if (
        progress.get("completed_at") is not None
        or not finite(started, now)
        or type(total) is not int
        or total <= 0
    ):
        return None
    expected = total / MIN_DOWNLOAD_BYTES_PER_SECOND + DOWNLOAD_OVERDUE_SLACK_SECONDS
    if now - started <= expected:
        return None
    observed, last = progress.get("last_observed_at"), progress.get("last_progress_at")
    if not finite(observed, now) or now - observed > 300:
        return started, "download_unobserved"
    if not finite(last, now) or now - last >= 300:
        return started, "download_stalled"
    return started, "download_slow"


def worker_unreachable(binding, workers, now):
    """A machine that served before but has no live worker and no fresh ready runtime.

    After an app restart or deploy a dial-out worker can stay paid while its
    registration expires and its runtime reads stay unconfirmed (#1140). It
    takes no work, so with matching work queued it must stop, not bill idle.
    """
    if not finite(binding.get("first_runtime_ready_at"), now):
        return False
    lost_since = []
    for worker in workers:
        expires = worker.get("expires_at")
        if type(expires) not in (int, float):
            continue
        if worker.get("state") in {"ready", "busy"} and expires > now:
            return False
        lost_since.append(min(expires, now))
    # Only a runtime the controller read and could not confirm counts as lost.
    # A "ready" binding that is merely old means nobody looked (for example the
    # controller itself was down), never that the worker is gone.
    if binding.get("state") in {"ready", "busy"}:
        return False
    # Stop only once the last sign of life is older than the grace window.
    last = max(lost_since, default=None)
    return last is None or now - last >= WORKER_LOST_GRACE_SECONDS


def finite(value, now):
    return type(value) in (int, float) and math.isfinite(value) and 0 <= value <= now


def finding(
    code, resource, since, diagnosis, *, node_ids=(), stop_replace=False, detail=None
):
    value = {
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
    if detail:
        # The exact underlying reason code (blocker, top claim refusal).
        value["detail"] = detail
    return value


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


def node_attempts(node, attempt_rows, workers):
    """Workers and attempts that belong to one original node."""
    binding = node["payload"].get("dstack", {})
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
    return associated, related


def unserved_stop(node, related, now):
    """(started, stopped) of a start that ended without serving, else None."""
    binding = node["payload"].get("dstack", {})
    started = binding.get("apply_started_at")
    stopped = binding.get("first_stopped_at")
    served = any(
        finite(attempt.get("submission_started_at"), now) for attempt in related
    )
    if (
        node["runtime_state"] == "stopped"
        and finite(started, now)
        and finite(stopped, now)
        and started <= stopped
        and not served
    ):
        return started, stopped
    return None


def start_loop(cycles):
    """A machine that was ready but never served pauses demand; failed starts do not.

    `cycles` are (stopped, intent_id, lane) in the loop window; lane is None
    unless an allocated machine never became ready. A start that never got
    ready is the host's or the lane's fault: the host is denied for good
    (host_policy.deny_failed_rental) and the lane cools on its own
    (scheduling.health), so its cost is bounded and it must not stop automatic
    start for everyone (owner, 2026-11-06). Four such starts in an hour used
    to pause demand; on 2026-11-10 that stopped staging and nothing switched
    it back on. The global switch is for the loop that is ours: machines boot,
    are ready, and are never given work.
    """
    return len(cycles) >= 2 and any(backend is None for _, _, backend in cycles)


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
        associated, related = node_attempts(node, attempt_rows, workers)
        served = [
            attempt
            for attempt in related
            if finite(attempt.get("submission_started_at"), now)
        ]
        cycle = unserved_stop(node, related, now)
        # A start the provider never allocated keeps demand start's own
        # no-allocation backoff; it is not a start/stop loop (#1034
        # two_no_capacity_starts paused demand for the whole tenant).
        allocated = node.get("provider_instance_id") or binding.get(
            "provider_instance_id"
        )
        if (
            demand_enabled
            and cycle is not None
            and (allocated or finite(binding.get("first_runtime_ready_at"), now))
            and now - loop_window_seconds <= cycle[0]
            and (
                not finite(demand_enabled_since, now)
                or cycle[1] >= demand_enabled_since
            )
        ):
            # Attributed to a lane only for an allocated machine that never
            # became ready; that lane then cools down alone (scheduling.health).
            from .scheduling.health import lane

            ready_at = binding.get("first_runtime_ready_at")
            # A machine that became ready after the queue drained, and idled
            # out, is a start overshoot, not a broken dispatch loop. Count it
            # only if matching work that was already waiting when it became
            # ready is still waiting now (staging 2026-11-09, paused demand).
            overshoot = finite(ready_at, now) and not any(
                matching(node, job)
                and finite(
                    since := queue_ages.get(job["id"], job.get("created_at")), now
                )
                and since <= ready_at
                for job in queued
            )
            if not overshoot:
                empty_cycles.append((cycle[1], node["intent_id"], lane(node, now)))
        if (
            node["desired_state"] != "running"
            or binding.get("apply_started") is not True
        ):
            continue
        overdue = download_overdue(binding, now)
        if overdue is not None:
            result.append(
                finding(
                    "capacity_download_overdue",
                    node["intent_id"],
                    overdue[0],
                    overdue[1],
                    node_ids=(node["intent_id"],),
                )
            )
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
            else "worker_unreachable"
            if worker_unreachable(binding, associated, now)
            else "dispatch_unobserved"
        )
        result.append(
            finding(
                "capacity_paid_idle_queue",
                node["intent_id"],
                since,
                diagnosis,
                node_ids=(node["intent_id"],),
                stop_replace=diagnosis in STOP_DIAGNOSES,
            )
        )
    empty_cycles = sorted(set(empty_cycles))
    if start_loop(empty_cycles):
        last = empty_cycles[-2:]
        result.append(
            finding(
                "capacity_demand_start_loop",
                last[-1][1],
                last[0][0],
                "unserved_start_stop_loop",
                node_ids=[node for _, node, _ in last],
            )
        )
    return result


def queue_episode_ages(connection, tenant):
    """Preparation claim/defer cycles are one wait until genuine submission."""
    waiting = ("waiting_capacity", "queued", "claimed")
    status = outbox.c.payload["public_state"]["status"].as_string()
    boundary = (
        select(
            outbox.c.aggregate_id.label("job_id"),
            func.max(outbox.c.job_event_seq).label("sequence"),
        )
        .join(jobs, outbox.c.aggregate_id == jobs.c.id)
        .where(
            jobs.c.tenant_id == tenant,
            jobs.c.status.in_(QUEUED),
            status.is_not(None),
            status.not_in(waiting),
        )
        .group_by(outbox.c.aggregate_id)
        .subquery()
    )
    rows = (
        select(jobs.c.id, func.min(outbox.c.created_at).label("since"))
        .select_from(
            jobs.join(outbox, outbox.c.aggregate_id == jobs.c.id).outerjoin(
                boundary, boundary.c.job_id == jobs.c.id
            )
        )
        .where(
            jobs.c.tenant_id == tenant,
            jobs.c.status.in_(QUEUED),
            status.in_(waiting),
            outbox.c.job_event_seq > func.coalesce(boundary.c.sequence, 0),
        )
        .group_by(jobs.c.id)
    )
    return {row.id: row.since for row in connection.execute(rows)}


def capacity_rows(connection, tenant):
    """Original nodes (with provider), attempts and workers, bounded."""
    from .dstack_demand import _nodes

    nodes = _nodes(connection, tenant)
    providers = {
        row.id: row.provider
        for row in connection.execute(
            select(instance_intents.c.id, instance_intents.c.provider)
        )
    }
    for node in nodes:
        node["provider"] = providers[node["intent_id"]]
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
    return nodes, attempt_rows, workers


def snapshot(service, connection, now):
    from .dstack_demand_policy import policy_key, read_runtime

    tenant = service.settings.tenant_id
    nodes, attempt_rows, workers = capacity_rows(connection, tenant)
    job_rows = [
        dict(row)
        for row in connection.execute(
            select(jobs).where(jobs.c.tenant_id == tenant).limit(10001)
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
        queue_ages=queue_episode_ages(connection, tenant),
        demand_enabled=enabled,
        demand_enabled_since=policy["updated_at"] if policy else None,
    )
    blocked = start_blocked(connection, tenant, nodes, job_rows, now)
    if blocked is not None:
        findings.append(blocked)
    findings.extend(collect_stuck(connection, job_rows, attempt_rows, now))
    for value in findings:
        if value["code"] == "capacity_paid_idle_queue":
            reason = top_refusal(connection, value["resource_id"], now)
            if reason:
                value["detail"] = reason
    return findings, policy


def collect_stuck(connection, job_rows, attempt_rows, now):
    """A finished run whose result collection keeps failing for 15 minutes.

    ``detail`` says whether the output is already stored outside the host
    (held, never regenerated: an operator finishes it) or the watchdog is
    still retrying before it reclaims the attempt (capacity_collect.py).
    """
    from .capacity_collect import stuck_collections

    return [
        finding(
            "capacity_collect_stuck",
            job["id"],
            since,
            "collect_stuck",
            detail="collection_output_stored"
            if stored
            else attempt.get("error_code") or "collection_failed",
        )
        for job, attempt, since, stored in stuck_collections(
            connection, job_rows, attempt_rows, now
        )
    ]


def start_blocked(connection, tenant, nodes, job_rows, now):
    """Queued demand work, no live machine and starts refused for 30 minutes.

    The demand timeline (capacity_timeline.py) holds the exact blocker code and
    since when the queue has had no machine; this only reads its latest entry.
    """
    from .capacity_timeline import EVENT, demand_key

    if any(node["desired_state"] == "running" for node in nodes) or not any(
        job["status"] in QUEUED
        and isinstance(job.get("execution_plan"), dict)
        and job["execution_plan"].get("demand_start")
        for job in job_rows
    ):
        return None
    latest = connection.execute(
        select(outbox.c.payload)
        .where(
            outbox.c.event_type == EVENT,
            outbox.c.aggregate_id == demand_key(tenant),
        )
        .order_by(outbox.c.created_at.desc())
        .limit(1)
    ).scalar()
    value = latest.get("value") if isinstance(latest, dict) else None
    if not isinstance(value, dict) or value.get("stalled") is not True:
        return None
    since = value.get("stalled_since")
    if not finite(since, now) or now - since <= START_BLOCKED_SECONDS:
        return None
    blocker = value.get("detail_code") or value.get("reason_code") or value.get("state")
    return finding(
        "capacity_start_blocked",
        demand_key(tenant),
        since,
        "start_blocked",
        detail=blocker if isinstance(blocker, str) else "unknown",
    )


def top_refusal(connection, intent_id, now):
    """The most frequent claim refusal on a machine in the last 15 minutes."""
    from .capacity_timeline import EVENT

    for payload in connection.execute(
        select(outbox.c.payload)
        .where(
            outbox.c.event_type == EVENT,
            outbox.c.aggregate_id == intent_id,
            outbox.c.created_at >= now - 900,
        )
        .order_by(outbox.c.created_at.desc())
        .limit(50)
    ).scalars():
        if isinstance(payload, dict) and payload.get("code") == "claim_refused":
            reasons = payload.get("detail", {}).get("reasons") or {}
            if reasons:
                return max(sorted(reasons), key=lambda name: reasons[name])
    return None


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
        if code == "capacity_start_blocked":
            # One alert per blocked episode; the resource is the demand stream.
            previous = connection.execute(
                select(outbox.c.id)
                .where(
                    outbox.c.event_type == "capacity.health_alert",
                    outbox.c.aggregate_id == value["resource_id"],
                    outbox.c.payload["episode"].as_string() == value["episode"],
                )
                .limit(1)
            ).first()
            return None if previous else _event(connection, tenant, value, now)
        table, key = (
            (jobs, jobs.c.id)
            if code in JOB_ALERTS
            else (operator_nodes, operator_nodes.c.intent_id)
        )
        if table is operator_nodes:
            repo._lock_capacity(connection)
        row = repo._locked(connection, select(table).where(key == value["resource_id"]))
        if row is None:
            return None
        if table is jobs:
            if row["tenant_id"] != tenant or row["status"] not in JOB_ALERTS[code]:
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
            if (
                value["stop_replace"]
                and previous.get("diagnosis") != value["diagnosis"]
            ):
                # The same idle episode can turn into a stop diagnosis later,
                # e.g. a worker that never re-registers after a deploy (#1140).
                health[code] = {**previous, "diagnosis": value["diagnosis"]}
                connection.execute(
                    update(table)
                    .where(key == value["resource_id"])
                    .values(payload=container)
                )
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
                    if value["code"] in JOB_RESOURCE_ALERTS
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
