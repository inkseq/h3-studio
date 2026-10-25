"""Owned presentation groups and read projections over the original dstack ledger.

Groups never allocate capacity, schedule jobs or settle bills. All lifecycle
commands continue through DstackOperator's original journals and spend checks.
"""

from __future__ import annotations

import copy
import math
import uuid

from sqlalchemy import Column, Float, String, Table, insert, select, update

from .control import WorkerControl
from .dstack_operator import BACKEND_MARKER, _actor, _finite
from .operator_capacity import (
    OperatorError,
    advance_command_revision,
    command_revision,
    operator_commands,
    operator_nodes,
    operator_policy,
    require,
    safe_id,
)
from .repository import (
    BudgetExceeded,
    attempts,
    budget_accounts,
    instance_intents,
    jobs,
    metadata,
    outbox,
    registered_workers,
    request_hash,
)
from .worker_admission import worker_window_reason

operator_groups = Table(
    "platform_operator_machine_groups",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("tenant_id", String(200), nullable=False),
    Column("owner_id", String(200), nullable=False),
    Column("name", String(80), nullable=False),
    Column("created_at", Float, nullable=False),
    Column("updated_at", Float, nullable=False),
)


def is_admin(settings, principal, repository):
    from .identity import is_admin as canonical_admin

    return canonical_admin(principal, repository, settings)


def owned_group(connection, settings, owner, group_id):
    """Presentation ownership check usable by the ordinary preview/start path."""
    if group_id is None:
        return None
    require(safe_id(group_id), "dstack_group_invalid", 422)
    row = (
        connection.execute(
            select(operator_groups).where(
                operator_groups.c.id == group_id,
                operator_groups.c.tenant_id == settings.tenant_id,
                operator_groups.c.owner_id == owner,
            )
        )
        .mappings()
        .first()
    )
    require(row is not None, "dstack_group_not_found", 404)
    return dict(row)


def advance_group_version(connection, settings, owner, group_id, now):
    """Advance membership even when consecutive changes share one clock tick.

    Callers hold the original capacity lock. The timestamp is also the opaque
    group version; a frozen/coarse clock must not hide a new member from a
    previously reviewed group stop/hold command.
    """
    group = owned_group(connection, settings, owner, group_id)
    version = max(now, math.nextafter(group["updated_at"], math.inf))
    connection.execute(
        update(operator_groups)
        .where(
            operator_groups.c.id == group_id,
            operator_groups.c.tenant_id == settings.tenant_id,
            operator_groups.c.owner_id == owner,
        )
        .values(updated_at=version)
    )
    return version


def _nodes(connection, settings, owners=None):
    result = []
    for row in connection.execute(select(operator_nodes)).mappings():
        value = row["payload"]
        if (
            value.get("capacity_backend") == BACKEND_MARKER
            and value.get("tenant_id") == settings.tenant_id
            and (owners is None or value.get("owner_id") in owners)
        ):
            result.append(dict(row))
    return result


def _workers(connection, intent):
    if not intent.get("provider_instance_id"):
        return []
    return [
        dict(row)
        for row in connection.execute(
            select(registered_workers).where(
                registered_workers.c.provider == intent["provider"],
                registered_workers.c.instance_id == intent["provider_instance_id"],
            )
        ).mappings()
    ]


def _waiting(connection, settings, owners):
    result = []
    rows = connection.execute(
        select(jobs)
        .where(
            jobs.c.tenant_id == settings.tenant_id,
            jobs.c.owner_id.in_(owners),
            jobs.c.status.in_(("queued", "waiting_capacity")),
        )
        .order_by(jobs.c.created_at)
    ).mappings()
    for row in rows:
        plan = row["execution_plan"]
        if (
            not isinstance(plan, dict)
            or plan.get("dispatch_backend") != "hatchet-v1"
            or not isinstance(row["request"], dict)
        ):
            continue
        if row["current_attempt_id"]:
            if not WorkerControl._proven_unsubmitted_queue(connection, row):
                continue
        elif row["lease_worker_id"] or row["lease_expires_at"] is not None:
            continue
        unsafe = connection.execute(
            select(attempts.c.id).where(
                attempts.c.job_id == row["id"],
                attempts.c.submission_started_at.is_not(None),
                attempts.c.upstream_stopped != 1,
            )
        ).first()
        if unsafe is None:
            result.append(dict(row))
    return result


def _mode(job):
    stored = job.get("request", {})
    effective = stored.get("request", stored) if isinstance(stored, dict) else {}
    value = effective.get("mode") if isinstance(effective, dict) else None
    return value if value in {"fl", "ref"} else None


def _matches(node, intent, job):
    payload, binding = node["payload"], node["payload"].get("dstack", {})
    # Rental funding belongs to one project; the owner's creation projects can
    # use the same compatible worker. Match the actual claim contract without
    # dropping tenant/owner isolation or the exact profile/engine checks.
    if (
        job["tenant_id"] != payload.get("tenant_id")
        or job["owner_id"] != payload.get("owner_id")
        or job["request"].get("deployment_profile_id") != binding.get("profile_id")
    ):
        return False
    from .runtime_catalog import profile_output_delivery

    spec = {
        "pool": intent["pool"],
        "backend": "wangp-worker",
        "dispatch_backend": "hatchet-v1",
        "output_delivery": profile_output_delivery(binding["profile_id"]),
        "recipe_ids": [
            {"fl": "h3-base-fl2va-v1", "ref": "h3-base-ref2va-v1"}.get(
                binding.get("mode")
            )
        ],
        "model_id": binding.get("model_id"),
        "configuration_id": binding.get("configuration_id"),
        "engine_manifest_digest": binding.get("manifest_digest"),
    }
    return _mode(job) == binding.get("mode") and WorkerControl.matches(
        {"spec": spec}, job
    )


def _compatible(connection, node, intent, job, now):
    binding = node["payload"].get("dstack", {})
    observed = binding.get("observed_at")
    fresh = node["payload"].get("observation_fresh_seconds", 30)
    if (
        node["desired_state"] != "running"
        or node["runtime_state"] not in {"ready", "busy"}
        or not _finite(observed)
        or not _finite(fresh)
        or not 0 <= now - observed <= fresh
        or not _matches(node, intent, job)
    ):
        return False
    for worker in _workers(connection, intent):
        if (
            worker["state"] in {"ready", "busy", "leased"}
            and not worker["drain_requested"]
            and worker["expires_at"] > now
            and WorkerControl.matches(worker, job)
            and worker_window_reason(
                connection,
                worker,
                now,
                expected_runtime_s=job["expected_runtime_s"],
                deployment_profile_id=job["request"].get("deployment_profile_id"),
            )
            is None
        ):
            return True
    return False


def stop_impact(connection, settings, node_ids, now):
    """Advisory under the caller's lock; no queue or provider mutation.

    Fingerprints bind exact affected jobs and target membership. A busy but
    qualified compatible machine remains future capacity; preparing/stale hosts
    cannot conceal the loss of the last executable slot.
    """
    selected = set(node_ids)
    rows = _nodes(connection, settings)
    targets = [row for row in rows if row["intent_id"] in selected]
    owners = {row["payload"]["owner_id"] for row in targets}
    intents = {
        row["id"]: dict(row)
        for row in connection.execute(select(instance_intents)).mappings()
    }
    affected = {"fl": [], "ref": []}
    for job in _waiting(connection, settings, owners):
        mode = _mode(job)
        if mode is None or not any(
            _matches(node, intents[node["intent_id"]], job) for node in targets
        ):
            continue
        if not any(
            node["intent_id"] not in selected
            and _compatible(connection, node, intents[node["intent_id"]], job, now)
            for node in rows
        ):
            affected[mode].append(job["id"])
    warnings = [
        {
            "mode": mode,
            "waiting_jobs": len(ids),
            "waiting_job_ids": sorted(ids),
            "message": f"{len(ids)} {'FL' if mode == 'fl' else 'REF'} jobs will have no machine",
        }
        for mode, ids in affected.items()
        if ids
    ]
    return {
        "warnings": warnings,
        "fingerprint": request_hash(
            {
                "node_ids": sorted(selected),
                "affected_jobs": {
                    mode: sorted(ids) for mode, ids in affected.items() if ids
                },
            }
        ),
    }


def _admin_download_details(node, now):
    """Completed-content averages are not measured network throughput."""
    from .dstack_preparation import download_progress_timing
    from .host_policy import bound_observation

    physical = bound_observation(dict(node), now=now) or {}
    timing = download_progress_timing(node["payload"].get("dstack", {}), now)
    duration = timing["duration_seconds"] if timing else None
    completed = timing["bytes_complete"] if timing else None
    return {
        "host": {
            "country": physical.get("country"),
            "region": physical.get("region"),
            "advertised_download_mbps": physical.get("advertised_download_mbps"),
            "bandwidth_basis": "provider_advertised",
            "observed_at": physical.get("observed_at"),
            "measured_download_mbps": None,
        },
        "completed_content_download": {
            "average_mbps": completed * 8 / 1_000_000 / duration
            if duration is not None and duration > 0 and completed is not None
            else None,
            "basis": "completed_files",
            "duration_seconds": duration,
            "bytes_complete": completed,
            "bytes_total": timing["bytes_total"] if timing else None,
        },
    }


# Ended machines' usage and metrics change only when billing settles, yet the GPU console
# polls every 5 s and each node costs several metric queries over jobs/artifacts/outbox
# (~100 historical nodes kept the 2-vCPU host busy). Reuse an ended node's result for
# ENDED_METRICS_TTL within the same Sydney day; live nodes are always recomputed.
ENDED_METRICS_TTL = 600
_ENDED_METRICS = {}
DEMAND_SUMMARY_TTL = 15


def _cached_metrics(compute, connection, original, intent, workers, now):
    ended = (
        original.get("desired_state") == "stopped"
        and original.get("runtime_state") == "stopped"
    )
    if not ended or not _finite(now):
        return compute(connection, original, intent, workers, now)
    key = (
        original.get("intent_id"),
        original.get("updated_at"),
        tuple(sorted(str(worker.get("id")) for worker in workers)),
        int((now + 10 * 3600) // 86400),  # Sydney-ish day bucket; TTL bounds the edge.
    )
    hit = _ENDED_METRICS.get(key)
    if hit is not None and 0 <= now - hit[0] < ENDED_METRICS_TTL:
        return copy.deepcopy(hit[1])
    value = compute(connection, original, intent, workers, now)
    if len(_ENDED_METRICS) > 4096:
        _ENDED_METRICS.clear()
    _ENDED_METRICS[key] = (now, copy.deepcopy(value))
    return value


class DstackWorkspace:
    def __init__(self, service):
        self.service, self.repo, self.settings = service, service.repo, service.settings

    def _authorize(self, principal, *, admin=False):
        owner = self.service.authorize(principal)
        if admin:
            self.service.authorize_admin(principal)
        return owner

    def _journal(self, connection, principal, key, kind, body):
        actor = _actor(self.settings.tenant_id, principal.owner)
        digest = request_hash({"kind": kind, "body": body})
        return (
            actor,
            digest,
            self.service._existing_command(connection, actor, key, digest),
        )

    def _command(
        self, connection, actor, key, digest, kind, payload, *, state="completed"
    ):
        identity, now = str(uuid.uuid4()), self.repo.clock()
        connection.execute(
            insert(operator_commands).values(
                id=identity,
                actor=actor,
                idempotency_key=key,
                request_hash=digest,
                kind=kind,
                state=state,
                payload=payload,
                created_at=now,
                updated_at=now,
            )
        )
        return identity

    def _display_demand_start(self, connection, now):
        # The overview's demand panel asks dstack about every owned run; a poll
        # within DEMAND_SUMMARY_TTL reuses it. Starts/admission never read this.
        hit = getattr(self, "_demand_summary", None)
        if hit is not None and _finite(now) and 0 <= now - hit[0] < DEMAND_SUMMARY_TTL:
            return copy.deepcopy(hit[1])
        value = self.service.demand_start_summary(connection, now)
        if _finite(now):
            self._demand_summary = (now, copy.deepcopy(value))
        return value

    def demand_start(self, principal):
        self._authorize(principal, admin=True)
        with self.repo.engine.connect() as connection:
            return self.service.demand_start_summary(connection, self.repo.clock())

    def set_demand_start(self, principal, body, key):
        self._demand_summary = None
        try:
            return self._set_demand_start(principal, body, key)
        finally:
            self._demand_summary = None

    def _set_demand_start(self, principal, body, key):
        from .dstack_demand_policy import load_runtime, policy_key
        from .identity import account_identity, lock_account_gate

        self._authorize(principal, admin=True)
        require(
            isinstance(body, dict)
            and set(body) == {"expected_revision", "enabled", "reason"}
            and (
                body["expected_revision"] is None
                or (
                    isinstance(body["expected_revision"], str)
                    and len(body["expected_revision"]) == 64
                    and all(c in "0123456789abcdef" for c in body["expected_revision"])
                )
            )
            and type(body["enabled"]) is bool
            and isinstance(body["reason"], str)
            and body["reason"] == body["reason"].strip()
            and 1 <= len(body["reason"]) <= 500
            and not any(ord(c) < 32 or ord(c) == 127 for c in body["reason"]),
            "dstack_demand_policy_invalid",
            422,
        )
        with self.repo.transaction() as connection:
            lock_account_gate(connection, self.settings.tenant_id)
            self.repo._lock_capacity(connection)
            # Authorization precedes replay too: a demoted administrator must
            # not recover privileged results through an old command key.
            identity = account_identity(
                connection, self.settings.tenant_id, principal.owner
            )
            require(
                identity is None
                or (
                    identity.get("status") == "active"
                    and identity.get("role") == "admin"
                ),
                "admin_required",
                403,
            )
            actor, digest, prior = self._journal(
                connection, principal, key, "dstack_demand_set", body
            )
            if prior:
                return copy.deepcopy(prior["payload"])
            old, version = load_runtime(connection, self.settings.tenant_id)
            require(
                old["revision"] == body["expected_revision"],
                "dstack_demand_policy_conflict",
            )
            now = self.repo.clock()
            stored = {
                "version": version + 1,
                "payload": {
                    "tenant_id": self.settings.tenant_id,
                    "enabled": body["enabled"],
                },
                "actor": principal.actor_id,
                "updated_at": now,
            }
            identity = policy_key(self.settings.tenant_id)
            if version:
                changed = connection.execute(
                    update(operator_policy)
                    .where(
                        operator_policy.c.id == identity,
                        operator_policy.c.version == version,
                    )
                    .values(**stored)
                )
                require(changed.rowcount == 1, "dstack_demand_policy_conflict")
            else:
                connection.execute(
                    insert(operator_policy).values(id=identity, **stored)
                )
            result, _ = load_runtime(connection, self.settings.tenant_id)
            command_id = self._command(
                connection, actor, key, digest, "dstack_demand_set", result
            )
            connection.execute(
                insert(outbox).values(
                    id=str(uuid.uuid4()),
                    event_type="operator.demand_start_updated",
                    aggregate_id=identity,
                    payload={
                        "tenant_id": self.settings.tenant_id,
                        "actor_id": principal.actor_id,
                        "actor_owner": principal.owner,
                        "reason": body["reason"],
                        "command_id": command_id,
                        "old": old,
                        "new": result,
                        "old_version": version,
                        "new_version": version + 1,
                    },
                    created_at=now,
                )
            )
            return result

    def create_group(self, principal, body, key):
        owner = self._authorize(principal)
        name = body.get("name") if isinstance(body, dict) else None
        require(
            isinstance(body, dict)
            and set(body) == {"name"}
            and isinstance(name, str)
            and name == name.strip()
            and 1 <= len(name) <= 80
            and not any(ord(c) < 32 for c in name),
            "dstack_group_invalid",
            422,
        )
        with self.repo.transaction() as connection:
            self.repo._lock_capacity(connection)
            actor, digest, prior = self._journal(
                connection, principal, key, "dstack_group_create", body
            )
            if prior:
                return copy.deepcopy(prior["payload"])
            identity, now = str(uuid.uuid4()), self.repo.clock()
            connection.execute(
                insert(operator_groups).values(
                    id=identity,
                    tenant_id=self.settings.tenant_id,
                    owner_id=owner,
                    name=name,
                    created_at=now,
                    updated_at=now,
                )
            )
            result = {
                "id": identity,
                "name": name,
                "owner_id": owner,
                "version": now,
                "node_ids": [],
                "hourly_cost_microusd": 0,
            }
            self._command(connection, actor, key, digest, "dstack_group_create", result)
            return result

    def move(self, principal, node_id, body, key):
        owner = self._authorize(principal)
        require(
            isinstance(body, dict)
            and "group_id" in body
            and not set(body) - {"group_id", "expected_version"}
            and ("expected_version" not in body or _finite(body["expected_version"])),
            "dstack_group_move_invalid",
            422,
        )
        with self.repo.transaction() as connection:
            self.repo._lock_capacity(connection)
            actor, digest, prior = self._journal(
                connection,
                principal,
                key,
                "dstack_group_move",
                {"node_id": node_id, **body},
            )
            if prior:
                return copy.deepcopy(prior["payload"])
            # Admin workspace grants stop authority, not moving another user's
            # machines into the administrator's presentation groups.
            node = self.repo._locked(
                connection,
                select(operator_nodes).where(operator_nodes.c.intent_id == node_id),
            )
            require(
                node is not None
                and node["payload"].get("capacity_backend") == BACKEND_MARKER
                and node["payload"].get("tenant_id") == self.settings.tenant_id
                and node["payload"].get("owner_id") == owner,
                "dstack_node_not_found",
                404,
            )
            group = owned_group(connection, self.settings, owner, body["group_id"])
            require(
                "expected_version" not in body
                or body["expected_version"] == command_revision(node["payload"]),
                "operator_node_version_conflict",
            )
            now = self.repo.clock()
            payload = advance_command_revision(copy.deepcopy(node["payload"]))
            previous = payload.get("group_id")
            payload["group_id"] = body["group_id"]
            connection.execute(
                update(operator_nodes)
                .where(operator_nodes.c.intent_id == node_id)
                .values(payload=payload, updated_at=now)
            )
            for identity in {previous, body["group_id"]} - {None}:
                advance_group_version(connection, self.settings, owner, identity, now)
            result = {
                "node_id": node_id,
                "group_id": body["group_id"],
                "group_name": group["name"] if group else None,
                "version": command_revision(payload),
            }
            self._command(connection, actor, key, digest, "dstack_group_move", result)
            return result

    def _targets(self, connection, principal, *, node_id=None, group_id=None):
        owner = self._authorize(principal)
        if node_id:
            node = self.service._owned_node(
                connection, principal, node_id, allow_admin=True
            )
            return [node]
        owned_group(connection, self.settings, owner, group_id)
        return [
            node
            for node in _nodes(connection, self.settings, {owner})
            if node["payload"].get("group_id") == group_id
            and node["desired_state"] == "running"
            and node["runtime_state"] != "stopped"
        ]

    def impact(self, principal, *, node_id=None, group_id=None):
        self._authorize(principal)
        with self.repo.engine.connect() as connection:
            targets = self._targets(
                connection, principal, node_id=node_id, group_id=group_id
            )
            return stop_impact(
                connection,
                self.settings,
                [node["intent_id"] for node in targets],
                self.repo.clock(),
            )

    def state(self, principal, *, admin=False, live_only=False):
        from .dstack_metrics import machine_metrics
        from .dstack_preparation import preparation_view

        owner = self._authorize(principal, admin=admin)
        from .identity import operator_owners

        owners = operator_owners(self.repo, self.settings) if admin else {owner}
        now = self.repo.clock()
        projected = {}
        # Delegation is read-only and follows an explicit administrator guard.
        # Browser authentication identity remains unchanged on every mutation.
        for account in sorted(owners):
            current = (
                self.service._state_for_owner(account)
                if admin
                else self.service.state(principal)
            )
            for node in current["nodes"]:
                projected[node["node_id"]] = {**node, "owner_id": account}
        result, groups = [], []
        with self.repo.engine.connect() as connection:
            demand_start = self._display_demand_start(connection, now) if admin else None
            # The preceding owner-state projection is advisory. A machine
            # accepted between those reads belongs to the next poll, never a
            # half-built row or a KeyError while dependent groups are rendered.
            originals = [
                node
                for node in _nodes(connection, self.settings, owners)
                if node["intent_id"] in projected
                # The admin overview needs live machines only; ended ones would each
                # cost several metric queries (~15 s for ~100 nodes on the small host).
                and not (
                    live_only
                    and node["desired_state"] == "stopped"
                    and node["runtime_state"] == "stopped"
                )
            ]
            # Readiness may have observed a newer heartbeat while this request
            # was in progress. Timestamp the captured rows after reading them;
            # do not reject a fresh observation as being in this request's future.
            now = self.repo.clock()
            intents = {
                row["id"]: dict(row)
                for row in connection.execute(select(instance_intents)).mappings()
            }
            all_groups = [
                dict(row)
                for row in connection.execute(
                    select(operator_groups).where(
                        operator_groups.c.tenant_id == self.settings.tenant_id,
                        operator_groups.c.owner_id.in_(owners),
                    )
                ).mappings()
            ]
            names = {group["id"]: group["name"] for group in all_groups}
            for original in originals:
                identity, payload = original["intent_id"], original["payload"]
                value = projected[identity]
                if (
                    original["desired_state"] != "running"
                    or original["runtime_state"] == "stopped"
                ):
                    value.update(ready=False, native_ready=False, slots_ready=0)
                workers = _workers(connection, intents[identity])
                expected_worker = "dstack-" + identity.replace("-", "")
                active = next(
                    (
                        worker
                        for worker in workers
                        if worker["id"] == expected_worker and worker["current_job_id"]
                    ),
                    None,
                )
                current = (
                    connection.execute(
                        select(jobs).where(jobs.c.id == active["current_job_id"])
                    )
                    .mappings()
                    .first()
                    if active
                    else None
                )
                value.update(
                    group_id=payload.get("group_id"),
                    group_name=names.get(payload.get("group_id")),
                    gpu_type=payload.get("selection", {}).get("gpu_type"),
                    gpu_count=1,
                    current_job=(
                        {
                            "job_id": current["id"],
                            "status": current["status"],
                            "mode": _mode(current),
                        }
                        if current is not None
                        and current["tenant_id"] == self.settings.tenant_id
                        and current["owner_id"] == payload["owner_id"]
                        and current["status"]
                        not in {"succeeded", "failed", "cancelled"}
                        else None
                    ),
                )
                value["lifecycle_phase"] = _phase(original, value)
                value["preparation"] = preparation_view(
                    original,
                    intents[identity],
                    value,
                    now,
                    fresh_seconds=payload.get("observation_fresh_seconds", 30),
                )
                if admin:
                    value.update(_admin_download_details(original, now))
                value.update(
                    _cached_metrics(
                        machine_metrics,
                        connection,
                        original,
                        intents[identity],
                        workers,
                        now,
                    )
                )
                result.append(value)
            for group in all_groups:
                members = [
                    node for node in result if node.get("group_id") == group["id"]
                ]
                groups.append(
                    {
                        "id": group["id"],
                        "name": group["name"],
                        "owner_id": group["owner_id"],
                        "version": group["updated_at"],
                        "node_ids": [node["node_id"] for node in members],
                        "hourly_cost_microusd": sum(
                            node["hourly_cost_microusd"]
                            for node in members
                            if node["lifecycle_phase"] != "stopped"
                        ),
                    }
                )
            waiting = _waiting(connection, self.settings, owners)
            queue = []
            for mode in ("fl", "ref"):
                items = [job for job in waiting if _mode(job) == mode]
                ready, busy, preparing, available_at = set(), set(), set(), {}
                # Heartbeats can change between READ COMMITTED statements.
                # Freeze each advisory eligibility result for this projection;
                # counting and lane estimation must use the same observations.
                eligibility = {
                    job["id"]: {
                        node["intent_id"]
                        for node in originals
                        if _compatible(
                            connection, node, intents[node["intent_id"]], job, now
                        )
                    }
                    for job in items
                }
                unserved = sum(not eligibility[job["id"]] for job in items)
                for node in originals:
                    identity = node["intent_id"]
                    if not any(_matches(node, intents[identity], job) for job in items):
                        continue
                    value = next(item for item in result if item["node_id"] == identity)
                    if any(identity in eligibility[job["id"]] for job in items):
                        (busy if value["current_job"] else ready).add(identity)
                        available_at[identity] = 0.0
                        if value["current_job"]:
                            current = (
                                connection.execute(
                                    select(jobs).where(
                                        jobs.c.id == value["current_job"]["job_id"]
                                    )
                                )
                                .mappings()
                                .first()
                            )
                            if current and _finite(current["expected_runtime_s"]):
                                started = connection.execute(
                                    select(
                                        attempts.c.submission_started_at,
                                        attempts.c.created_at,
                                    ).where(
                                        attempts.c.id == current["current_attempt_id"],
                                        attempts.c.job_id == current["id"],
                                    )
                                ).first()
                                stamp = (
                                    (
                                        started.submission_started_at
                                        or started.created_at
                                    )
                                    if started
                                    else None
                                )
                                elapsed = (
                                    max(0.0, now - stamp) if _finite(stamp) else 0.0
                                )
                                available_at[identity] = max(
                                    0.0, current["expected_runtime_s"] - elapsed
                                )
                    elif node["desired_state"] == "running" and value[
                        "lifecycle_phase"
                    ] in {
                        "renting",
                        "downloading_weights",
                        "loading_model",
                        "preparing",
                    }:
                        preparing.add(identity)
                last_start, complete = 0.0, 0.0
                if items and not unserved:
                    # Advisory FIFO projection, never another scheduler or
                    # claim. Each task uses only its actual compatible lane.
                    for job in items:
                        lanes = [
                            (available_at[identity], identity)
                            for identity in eligibility[job["id"]]
                            if identity in available_at
                        ]
                        available, lane = min(lanes)
                        start = max(available, max(0.0, job["not_before"] - now))
                        available_at[lane] = start + job["expected_runtime_s"]
                        last_start = max(last_start, start)
                        complete = max(complete, available_at[lane])
                rough = None if unserved else math.ceil(last_start)
                queue.append(
                    {
                        "mode": mode,
                        "waiting": len(items),
                        "ready": len(ready),
                        "busy": len(busy),
                        "preparing": len(preparing),
                        "unserved_waiting": unserved,
                        "rough_wait_seconds": rough,
                        "rough_complete_seconds": None
                        if unserved
                        else math.ceil(complete),
                        "estimate_basis": "compatible_fifo_last_start_runtime_quotes",
                        "jobs": [
                            {
                                "job_id": job["id"],
                                "owner_id": job["owner_id"],
                                "status": job["status"],
                                "runtime_profile_id": job["request"].get(
                                    "deployment_profile_id"
                                ),
                                "expected_runtime_seconds": job["expected_runtime_s"],
                            }
                            for job in items
                        ],
                    }
                )
            accounts = []
            if admin:
                for account in sorted(owners):
                    account_nodes = [
                        node for node in result if node["owner_id"] == account
                    ]
                    budgets = connection.execute(
                        select(budget_accounts).where(
                            budget_accounts.c.tenant_id == self.settings.tenant_id,
                            budget_accounts.c.owner_id == account,
                            budget_accounts.c.project_id.is_(None),
                        )
                    ).mappings()
                    today_known = all(
                        node["usage"]["today_cost_microusd"] is not None
                        for node in account_nodes
                    )
                    accounts.append(
                        {
                            "account": account,
                            "hourly_cost_microusd": sum(
                                node["hourly_cost_microusd"]
                                for node in account_nodes
                                if node["lifecycle_phase"] != "stopped"
                            ),
                            "today_cost_microusd": sum(
                                node["usage"]["today_cost_microusd"]
                                for node in account_nodes
                            )
                            if today_known
                            else None,
                            "cost_basis": "estimated_compute",
                            "spend_caps": [
                                {
                                    "limit_microusd": row["limit_microusd"],
                                    "spent_microusd": row["spent_microusd"],
                                    "reserved_microusd": row["reserved_microusd"],
                                }
                                for row in budgets
                            ],
                        }
                    )
        return {
            "operator": {
                "account": owner,
                "admin": is_admin(self.settings, principal, self.repo),
            },
            "capacity_backend": BACKEND_MARKER,
            "enabled": self.service.policy.get("enabled") is True,
            "observed_at": now,
            "nodes": result,
            "groups": groups,
            "queue": queue,
            **({"accounts": accounts, "demand_start": demand_start} if admin else {}),
        }

    def group_command(self, principal, group_id, body, key, kind):
        owner = self._authorize(principal)
        allowed = (
            {"expected_version", "acknowledge_capacity_loss"}
            if kind == "stop"
            else {
                "expected_version",
                "keep_on",
                "hold_seconds",
                "idle_shutdown_seconds",
            }
        )
        require(
            kind in {"stop", "hold"}
            and isinstance(body, dict)
            and not set(body) - allowed,
            "dstack_group_command_invalid",
            422,
        )
        require(
            "expected_version" not in body or _finite(body["expected_version"]),
            "dstack_group_command_invalid",
            422,
        )
        if kind == "hold":
            require(
                any(
                    field in body
                    for field in ("keep_on", "hold_seconds", "idle_shutdown_seconds")
                ),
                "dstack_group_command_invalid",
                422,
            )
        with self.repo.transaction() as connection:
            self.repo._lock_capacity(connection)
            actor, digest, prior = self._journal(
                connection,
                principal,
                key,
                "dstack_group_" + kind,
                {"group_id": group_id, **body},
            )
            if prior:
                command_id, payload = prior["id"], copy.deepcopy(prior["payload"])
                if prior["state"] == "completed":
                    return payload
            else:
                group = owned_group(connection, self.settings, owner, group_id)
                require(
                    "expected_version" not in body
                    or body["expected_version"] == group["updated_at"],
                    "operator_group_version_conflict",
                )
                targets = self._targets(connection, principal, group_id=group_id)
                ids = [node["intent_id"] for node in targets]
                impact = (
                    stop_impact(connection, self.settings, ids, self.repo.clock())
                    if kind == "stop"
                    else {"warnings": []}
                )
                if (
                    impact["warnings"]
                    and body.get("acknowledge_capacity_loss") != impact["fingerprint"]
                ):
                    raise CapacityLoss(impact)
                approved = sorted(
                    {
                        identity
                        for warning in impact["warnings"]
                        for identity in warning["waiting_job_ids"]
                    }
                )
                payload = {
                    "group_id": group_id,
                    "node_ids": ids,
                    "request": body,
                    "approved_job_ids": approved,
                    "child_requests": {},
                    "results": [],
                    "state": "accepted",
                }
                command_id = self._command(
                    connection,
                    actor,
                    key,
                    digest,
                    "dstack_group_" + kind,
                    payload,
                    state="accepted",
                )
        results = {item["node_id"]: item for item in payload["results"]}
        for node_id in payload["node_ids"]:
            if node_id in results:
                continue
            child_key = "group-" + request_hash(
                {"command": command_id, "node_id": node_id}
            )
            try:
                options = payload.get("child_requests", {}).get(node_id)
                if options is None:
                    if kind == "stop":
                        options = {}
                        with self.repo.engine.connect() as connection:
                            impact = stop_impact(
                                connection, self.settings, [node_id], self.repo.clock()
                            )
                        affected = {
                            identity
                            for warning in impact["warnings"]
                            for identity in warning["waiting_job_ids"]
                        }
                        if affected and affected.issubset(
                            set(payload["approved_job_ids"])
                        ):
                            options["acknowledge_capacity_loss"] = impact["fingerprint"]
                    else:
                        options = {
                            field: value
                            for field, value in body.items()
                            if field != "expected_version"
                        }
                # Freeze the exact child body before the original command.
                # A changed queue after response loss must never alter the
                # request hash bound to this child idempotency key.
                options = self._save_child_request(command_id, node_id, options)
                if kind == "stop":
                    value = self.service.node_command(
                        principal, node_id, options, child_key
                    )
                else:
                    value = self.service.set_hold(
                        principal, node_id, options, child_key
                    )
                results[node_id] = {
                    "node_id": node_id,
                    "state": value.get("state", "completed"),
                    **value,
                }
            except OperatorError as error:
                results[node_id] = {
                    "node_id": node_id,
                    "state": "rejected",
                    "code": error.code,
                }
            except BudgetExceeded:
                results[node_id] = {
                    "node_id": node_id,
                    "state": "rejected",
                    "code": "budget_limit_exceeded",
                }
            payload = self._save_child_result(command_id, node_id, results[node_id])
        with self.repo.transaction() as connection:
            original = self.repo._locked(
                connection,
                select(operator_commands).where(operator_commands.c.id == command_id),
            )
            payload = copy.deepcopy(original["payload"])
            payload["state"] = (
                "partial"
                if any(item.get("state") == "rejected" for item in payload["results"])
                else "completed"
            )
            payload["command_id"] = command_id
            connection.execute(
                update(operator_commands)
                .where(operator_commands.c.id == command_id)
                .values(
                    payload=copy.deepcopy(payload),
                    state="completed",
                    updated_at=self.repo.clock(),
                )
            )
        return payload

    def _save_child_request(self, command_id, node_id, options):
        with self.repo.transaction() as connection:
            original = self.repo._locked(
                connection,
                select(operator_commands).where(operator_commands.c.id == command_id),
            )
            payload = copy.deepcopy(original["payload"])
            requests = payload.setdefault("child_requests", {})
            if node_id not in requests:
                requests[node_id] = copy.deepcopy(options)
                connection.execute(
                    update(operator_commands)
                    .where(operator_commands.c.id == command_id)
                    .values(payload=payload, updated_at=self.repo.clock())
                )
            return copy.deepcopy(requests[node_id])

    def _save_child_result(self, command_id, node_id, result):
        with self.repo.transaction() as connection:
            original = self.repo._locked(
                connection,
                select(operator_commands).where(operator_commands.c.id == command_id),
            )
            payload = copy.deepcopy(original["payload"])
            results = {item["node_id"]: item for item in payload["results"]}
            # Concurrent observation cannot downgrade an accepted original
            # command to a later projection/validation rejection.
            if node_id not in results or results[node_id].get("state") == "rejected":
                results[node_id] = copy.deepcopy(result)
            payload["results"] = list(results.values())
            connection.execute(
                update(operator_commands)
                .where(operator_commands.c.id == command_id)
                .values(payload=payload, updated_at=self.repo.clock())
            )
            return payload


class CapacityLoss(OperatorError):
    def __init__(self, impact):
        super().__init__("capacity_loss_confirmation_required", 409)
        self.impact = impact
        self.capacity_loss = impact


def _phase(node, value):
    binding = node["payload"].get("dstack", {})
    if node["runtime_state"] == "stopped" or (
        binding.get("state") == "stopped"
        and binding.get("dstack_status") == "failed"
        and binding.get("reason_code") == "dstack_no_capacity_billing_pending"
        and binding.get("provider_instance_id") is None
    ):
        return "stopped"
    if node["desired_state"] == "stopped" or node["runtime_state"] in {
        "draining",
        "stopping",
    }:
        return "stopping"
    if value.get("current_job") or node["runtime_state"] == "busy":
        return "running_job"
    if value.get("ready"):
        return "ready"
    phase = {
        "model_download": "downloading_weights",
        "model_load": "loading_model",
    }.get(binding.get("bootstrap_phase"))
    if phase in {"downloading_weights", "loading_model"}:
        return phase
    if binding.get("dstack_status") in {"pending", "submitted", "provisioning"} or node[
        "runtime_state"
    ] in {"reserved", "creating"}:
        return "renting"
    return "preparing"
