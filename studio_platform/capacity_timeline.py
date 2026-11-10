"""Persistent per-machine capacity timeline and demand decisions (#1140).

Controller and app logs live a few hours (docker json-file, 5 MB x 2), and tick
results and refusal counts were never stored. When a paid machine misbehaves,
this timeline says where it got stuck and why, and it is the input for
``capacity_incident_export`` (an incident becomes a simulator scenario).

Recording only: the recorder runs after the controller tick has made every
decision, reads committed rows, writes outbox events and never changes a node,
job, worker, budget or provider. Any failure is logged and swallowed.

Events (``platform_outbox``, no new table):

- ``capacity.timeline`` aggregate ``<intent_id>``: one entry per lifecycle
  change (start requested, offer, allocation, run status, boot phase, download
  milestones every 25 %, model load, runtime ready, worker state, first claim,
  idle episode, claim refusal summary, stop request, removal, billing, intent).
- ``capacity.timeline`` aggregate ``demand:<tenant>``: demand decisions on
  change, at most DEMAND_BURST per window, the rest as per-code counts.
- ``capacity.provider_snapshot`` aggregate ``<intent_id>``: sanitized provider
  responses on change (capacity_snapshots.py).

Bounds: MAX_ENTRIES timeline and MAX_SNAPSHOTS snapshot rows per machine,
refusal counts per WINDOW_SECONDS, and RETENTION_SECONDS for both event types.
No owner, project, account, email, IP or credential is written.
"""

from __future__ import annotations

from collections import Counter
import hashlib
import json
import logging
import math
import uuid

from sqlalchemy import delete, func, insert, or_, select

from .capacity_snapshots import inventory_presence, install, safe_text
from .operator_capacity import operator_nodes
from .repository import (
    attempts,
    budget_reservations,
    instance_intents,
    outbox,
    registered_workers,
)

_logger = logging.getLogger(__name__)

EVENT = "capacity.timeline"
SNAPSHOT_EVENT = "capacity.provider_snapshot"
BACKEND_MARKER = "dstack-v1"
MAX_ENTRIES = 240
MAX_SNAPSHOTS = 80
WINDOW_SECONDS = 300
DEMAND_BURST = 6
IDLE_NOTE_SECONDS = 120
STALL_SECONDS = 300
PROBE_JOBS = 3
CLOSE_AFTER_SECONDS = 86400
RETENTION_SECONDS = 30 * 86400
PRUNE_INTERVAL_SECONDS = 3600
PRUNE_BATCH = 5000
WAITING = ("accepted", "observing")


def demand_key(tenant_id):
    return "demand:" + tenant_id


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def _age(now, value):
    return round(now - value, 1) if _finite(value) else None


def _code(value):
    return safe_text(value) if isinstance(value, str) else None


class _Track:
    """What was last written for one machine (or the demand stream)."""

    def __init__(self):
        self.last = {}
        self.entries = 0
        self.snapshots = 0
        self.snapshot_hash = {}
        self.refusals = Counter()
        self.refusals_since = None
        self.idle_since = None
        self.idle_noted = False
        self.probed_at = None
        self.closed = False
        self.window_start = None
        self.window_writes = 0
        self.suppressed = Counter()


class CapacityRecorder:
    def __init__(self, service, control):
        self.service, self.repo, self.control = service, service.repo, control
        self.tenant = getattr(getattr(service, "settings", None), "tenant_id", None)
        try:
            self.buffer = install(service, self.repo.clock)
        except Exception as error:
            # Snapshots are optional; the timeline still records without them.
            self.buffer = None
            _logger.warning(
                "capacity_snapshot_install_failed error_type=%s", type(error).__name__
            )
        self._tracks = {}
        self._pruned_at = None
        self._writes = []
        self._live = 0
        self._queued = 0

    # Public --------------------------------------------------------------
    def record_tick(self, result):
        """Persist what changed in this tick. Never raises, never decides."""
        if not isinstance(self.tenant, str):
            return
        try:
            self._writes = []
            now = self.repo.clock()
            with self.repo.engine.connect() as connection:
                self._queued = _queued(connection, self.tenant)
                self._record_nodes(connection, now)
                self._record_demand(connection, result.get("demand_start") or {}, now)
            self._flush(now)
            self._prune(now)
        except Exception as error:
            self._writes = []
            _logger.warning(
                "capacity_timeline_record_failed error_type=%s", type(error).__name__
            )

    # Storage -------------------------------------------------------------
    def _restore_demand(self, connection, key):
        """Only the latest demand entry matters for change detection."""
        if key in self._tracks:
            return
        track = self._tracks[key] = _Track()
        latest = connection.execute(
            select(outbox.c.payload)
            .where(outbox.c.event_type == EVENT, outbox.c.aggregate_id == key)
            .order_by(outbox.c.created_at.desc())
            .limit(1)
        ).scalar()
        if isinstance(latest, dict):
            track.last["demand"] = latest.get("value")

    def _restore(self, connection, keys):
        missing = [key for key in keys if key not in self._tracks]
        for start in range(0, len(missing), 200):
            chunk = missing[start : start + 200]
            for key in chunk:
                self._tracks[key] = _Track()
            rows = connection.execute(
                select(outbox.c.aggregate_id, outbox.c.event_type, outbox.c.payload)
                .where(
                    outbox.c.event_type.in_((EVENT, SNAPSHOT_EVENT)),
                    outbox.c.aggregate_id.in_(chunk),
                )
                .order_by(outbox.c.created_at, outbox.c.id)
            ).mappings()
            for row in rows:
                track, payload = self._tracks[row["aggregate_id"]], row["payload"]
                if row["event_type"] == SNAPSHOT_EVENT:
                    track.snapshots += 1
                    track.snapshot_hash[payload.get("kind")] = payload.get("hash")
                    continue
                track.entries += 1
                stage = payload.get("stage")
                if stage:
                    track.last[stage] = payload.get("value")
                if stage == "closed":
                    track.closed = True

    def _entry(self, key, stage, code, at, value=None, detail=None):
        track = self._tracks[key]
        # The demand stream is bounded by its burst window and retention.
        capped = not key.startswith("demand:")
        if capped and track.entries >= MAX_ENTRIES:
            return
        if capped and track.entries == MAX_ENTRIES - 1:
            stage, code, value, detail = "truncated", "timeline_truncated", None, {}
        track.entries += 1
        track.last[stage] = value
        self._writes.append(
            (
                EVENT,
                key,
                {
                    "version": 1,
                    "stage": stage,
                    "code": code,
                    "at": at,
                    "value": value,
                    "detail": detail or {},
                },
            )
        )

    def _changed(self, key, stage, value):
        track = self._tracks[key]
        return track.last.get(stage, "__unset__") != value

    def _flush(self, now):
        if not self._writes:
            return
        with self.repo.transaction() as connection:
            for index, (event_type, key, payload) in enumerate(self._writes):
                connection.execute(
                    insert(outbox).values(
                        id=str(uuid.uuid4()),
                        event_type=event_type,
                        aggregate_id=key,
                        payload=payload,
                        # Distinct stamps keep write order readable by time.
                        created_at=now + index * 1e-6,
                    )
                )
        self._writes = []

    def _prune(self, now):
        if (
            self._pruned_at is not None
            and now - self._pruned_at < PRUNE_INTERVAL_SECONDS
        ):
            return
        self._pruned_at = now
        with self.repo.transaction() as connection:
            ids = list(
                connection.execute(
                    select(outbox.c.id)
                    .where(
                        outbox.c.event_type.in_((EVENT, SNAPSHOT_EVENT)),
                        outbox.c.created_at < now - RETENTION_SECONDS,
                    )
                    .limit(PRUNE_BATCH)
                ).scalars()
            )
            if ids:
                connection.execute(delete(outbox).where(outbox.c.id.in_(ids)))

    # Machines ------------------------------------------------------------
    def _record_nodes(self, connection, now):
        nodes = [
            dict(row)
            for row in connection.execute(
                select(operator_nodes).where(
                    or_(
                        operator_nodes.c.runtime_state != "stopped",
                        operator_nodes.c.desired_state != "stopped",
                        operator_nodes.c.updated_at >= now - CLOSE_AFTER_SECONDS,
                    )
                )
            ).mappings()
            if row["payload"].get("capacity_backend") == BACKEND_MARKER
            and row["payload"].get("tenant_id") == self.tenant
        ]
        ids = [node["intent_id"] for node in nodes]
        self._restore(connection, ids)
        nodes = [node for node in nodes if not self._tracks[node["intent_id"]].closed]
        self._live = sum(node["desired_state"] == "running" for node in nodes)
        if not nodes:
            self._take_refusals({})
            return
        ids = [node["intent_id"] for node in nodes]
        intents = {
            row["id"]: dict(row)
            for row in connection.execute(
                select(instance_intents).where(instance_intents.c.id.in_(ids))
            ).mappings()
        }
        worker_ids = {_worker_id(intent_id): intent_id for intent_id in ids}
        workers = {
            row["id"]: dict(row)
            for row in connection.execute(
                select(registered_workers).where(
                    registered_workers.c.id.in_(list(worker_ids))
                )
            ).mappings()
        }
        claims = {
            row.worker_id: row
            for row in connection.execute(
                select(
                    attempts.c.worker_id,
                    func.count().label("claims"),
                    func.count(attempts.c.submission_started_at).label("submitted"),
                    func.min(attempts.c.created_at).label("first_at"),
                )
                .where(attempts.c.worker_id.in_(list(worker_ids)))
                .group_by(attempts.c.worker_id)
            )
        }
        reservations = {}
        for row in connection.execute(
            select(
                budget_reservations.c.reference_id,
                budget_reservations.c.state,
                budget_reservations.c.settled_at,
            ).where(
                budget_reservations.c.reference_type == "instance",
                budget_reservations.c.reference_id.in_(ids),
            )
        ):
            reservations.setdefault(row.reference_id, []).append(row)
        self._take_refusals(worker_ids)
        for node in nodes:
            intent = intents.get(node["intent_id"])
            if intent is None:
                continue
            worker = workers.get(_worker_id(node["intent_id"]))
            self._record_node(
                connection,
                node,
                intent,
                worker,
                claims.get(_worker_id(node["intent_id"])),
                reservations.get(node["intent_id"], []),
                now,
            )

    def _take_refusals(self, worker_ids):
        from .capacity_reasons import drain_workers

        for worker_id, counts in drain_workers().items():
            intent_id = worker_ids.get(worker_id)
            if intent_id is None:
                continue
            track = self._tracks[intent_id]
            if not track.refusals:
                track.refusals_since = self.repo.clock()
            track.refusals.update(counts)

    def _record_node(self, connection, node, intent, worker, claims, reserved, now):
        key = node["intent_id"]
        payload = node["payload"]
        binding = payload.get("dstack", {}) if isinstance(payload, dict) else {}
        request = payload.get("request", {})
        lifecycle = payload.get("lifecycle", {})
        if self._changed(key, "start", "requested"):
            guard = lifecycle.get("test_gpu_guard") or {}
            self._entry(
                key,
                "start",
                "start_requested",
                binding.get("apply_started_at") or intent.get("created_at"),
                "requested",
                {
                    "backend": _code(request.get("backend")),
                    "mode": _code(request.get("mode")),
                    "profile_id": _code(request.get("profile_id")),
                    "demand_job_id": _code(
                        payload.get("demand_start", {}).get("job_id")
                    ),
                    "cross_owner_fallback": payload.get("cross_owner_fallback"),
                    "test_task_id": _code(request.get("test_task_id")),
                    "expected_runtime_s": lifecycle.get("expected_runtime_s"),
                    "max_runtime_seconds": guard.get("max_runtime_seconds"),
                    "idle_shutdown_seconds": lifecycle.get("idle_shutdown_seconds"),
                    "observation_fresh_seconds": payload.get(
                        "observation_fresh_seconds"
                    ),
                    "max_price_microusd": request.get("max_price_microusd"),
                    "created_at": intent.get("created_at"),
                },
            )
        host = binding.get("host_observation")
        if isinstance(host, dict):
            offer = [_code(host.get("offer_id")), host.get("quoted_hourly_microusd")]
            if self._changed(key, "offer", offer):
                self._entry(
                    key,
                    "offer",
                    "offer_chosen",
                    host.get("observed_at")
                    if _finite(host.get("observed_at"))
                    else now,
                    offer,
                    {
                        name: (
                            _code(host.get(name))
                            if isinstance(host.get(name), str)
                            else host.get(name)
                        )
                        for name in (
                            "provider",
                            "gpu",
                            "gpu_count",
                            "region",
                            "country",
                            "quoted_hourly_microusd",
                            "advertised_download_mbps",
                            "reliability",
                            "offer_id",
                            "machine_id",
                        )
                    },
                )
        instance = intent.get("provider_instance_id") or binding.get(
            "provider_instance_id"
        )
        if instance and self._changed(key, "allocation", str(instance)):
            self._entry(
                key,
                "allocation",
                "allocated",
                now,
                str(instance),
                {
                    "provider_instance_id": _code(str(instance)),
                    "backend": _code(binding.get("backend")),
                },
            )
        status = binding.get("dstack_status")
        if isinstance(status, str) and self._changed(key, "run", status):
            self._entry(
                key,
                "run",
                "run_" + status,
                binding.get("observed_at")
                if _finite(binding.get("observed_at"))
                else now,
                status,
                {"reason_code": _code(binding.get("reason_code"))},
            )
        self._record_boot(key, binding, now)
        if binding.get("state") not in ("stopping", "stopped"):
            # A stopped host's progress is closed out, not a finished download.
            self._record_download(key, binding, now)
        self._record_worker(key, worker, claims, now)
        self._record_collect(connection, key, now)
        self._record_idle(connection, key, node, intent, binding, worker, now)
        self._record_stop(key, node, payload, binding, claims, now)
        self._record_snapshots(key, binding, instance, now)
        self._record_close(key, intent, binding, reserved, now)

    def _record_boot(self, key, binding, now):
        state, reason = binding.get("state"), binding.get("reason_code")
        if not isinstance(state, str):
            return
        served = _finite(binding.get("first_runtime_ready_at"))
        value = "serving" if served and state in ("ready", "busy") else [state, reason]
        if self._changed(key, "boot", value):
            self._entry(
                key,
                "boot",
                "serving" if value == "serving" else _code(reason) or state,
                now,
                value,
                {
                    "state": state,
                    "reason_code": _code(reason),
                    "phase": _code(binding.get("bootstrap_phase")),
                    "model_load_state": _code(binding.get("model_load_state")),
                },
            )
        phase = binding.get("bootstrap_phase")
        if isinstance(phase, str) and self._changed(key, "phase", phase):
            self._entry(key, "phase", "phase_" + phase, now, phase, {})
        ready = binding.get("first_runtime_ready_at")
        if _finite(ready) and self._changed(key, "ready", True):
            self._entry(key, "ready", "runtime_ready", ready, True, {})

    def _record_download(self, key, binding, now):
        from .dstack_preparation import MIN_DOWNLOAD_BYTES_PER_SECOND, download_rate

        progress = binding.get("download_progress")
        if not isinstance(progress, dict) or progress.get("valid") is not True:
            return
        total, done = progress.get("bytes_total"), progress.get("bytes_complete")
        if type(total) is not int or type(done) is not int or total <= 0:
            return
        complete = progress.get("completed_at") is not None
        last = progress.get("last_progress_at")
        stalled = not complete and _finite(last) and now - last >= STALL_SECONDS
        try:
            rate = download_rate(progress)
        except (KeyError, TypeError):
            rate = None
        slow = rate is not None and rate < MIN_DOWNLOAD_BYTES_PER_SECOND
        if rate is not None and self._changed(key, "measured", True):
            # Every host's measured rate, fast or slow, once per machine, so
            # the speed gate can be calibrated on the real spread (#903).
            host = binding.get("host_observation")
            host = host if isinstance(host, dict) else {}
            self._entry(
                key,
                "measured",
                "download_measured",
                now,
                True,
                {
                    "rate_bytes_per_s": round(rate),
                    "provider": _code(host.get("provider") or binding.get("backend")),
                    "host_id": _code(host.get("machine_id")),
                },
            )
        milestone = 4 if complete else min(3, int(4 * done / total))
        value = [milestone, complete, stalled, slow and not complete]
        if not self._changed(key, "download", value):
            return
        code = (
            "download_complete"
            if complete
            else "download_stalled"
            if stalled
            else "download_slow"
            if slow
            else "download_progress"
        )
        started = progress.get("started_at")
        self._entry(
            key,
            "download",
            code,
            progress.get("completed_at") if complete else now,
            value,
            {
                "bytes_complete": done,
                "bytes_total": total,
                "percent": round(100 * done / total, 1),
                "rate_bytes_per_s": round(rate) if rate is not None else None,
                "started_at": started if _finite(started) else None,
                "last_progress_age_s": _age(now, last),
            },
        )

    def _record_worker(self, key, worker, claims, now):
        if worker is not None:
            state = worker["state"]
            expired = worker["expires_at"] <= now and state not in (
                "unknown",
                "retired",
            )
            # Claims flip ready/leased/busy per job; one entry covers them all.
            serving = state in ("ready", "leased", "busy")
            value = "expired" if expired else "serving" if serving else state
            if self._changed(key, "worker", value):
                self._entry(
                    key,
                    "worker",
                    "worker_" + ("ready" if value == "serving" else value),
                    worker["expires_at"] if expired else now,
                    value,
                    {
                        "state": state,
                        "drain_requested": bool(worker["drain_requested"]),
                        "expires_at": worker["expires_at"],
                    },
                )
        if claims is not None and claims.claims and self._changed(key, "claim", True):
            self._entry(
                key,
                "claim",
                "first_claim",
                claims.first_at,
                True,
                {"claims": claims.claims, "submitted": claims.submitted},
            )

    def _record_collect(self, connection, key, now):
        """A failing result collection on this machine, and how it ended.

        One entry per attempt state: failing (with the output held outside
        the host or not), reclaimed (lost attempt, requeued) or collected.
        """
        from .capacity_collect import output_stored

        rows = connection.execute(
            select(
                attempts.c.id,
                attempts.c.job_id,
                attempts.c.status,
                attempts.c.error_code,
                attempts.c.collection_failures,
            ).where(
                attempts.c.worker_id == _worker_id(key),
                attempts.c.collection_failures > 0,
            )
        ).mappings()
        for row in rows:
            if row["status"] == "collecting":
                stored = output_stored(connection, row["job_id"], row["id"])
                code = "collect_output_stored" if stored else "collect_failed"
            elif row["status"] == "attempt_lost":
                code = "collect_reclaimed"
            else:
                code = "collect_" + _code(row["status"])
            stage = "collect:" + row["id"][:8]
            if self._changed(key, stage, code):
                self._entry(
                    key,
                    stage,
                    code,
                    now,
                    code,
                    {
                        "job_id": _code(row["job_id"]),
                        "attempt_id": _code(row["id"]),
                        "error_code": _code(row["error_code"]),
                        "failures": row["collection_failures"],
                    },
                )

    def _record_idle(self, connection, key, node, intent, binding, worker, now):
        track = self._tracks[key]
        # A booted machine without a job, including one whose runtime or
        # worker stopped answering: the probe then names why it cannot claim.
        idle = (
            node["desired_state"] == "running"
            and _finite(binding.get("first_runtime_ready_at"))
            and binding.get("state") not in ("busy", "stopping", "stopped")
            and (worker is None or worker["current_job_id"] is None)
        )
        if not idle:
            track.idle_since, track.idle_noted = None, False
        elif track.idle_since is None:
            track.idle_since = now
        probe_due = track.probed_at is None or now - track.probed_at >= WINDOW_SECONDS
        if (
            idle
            and now - track.idle_since >= IDLE_NOTE_SECONDS
            and (not track.idle_noted or probe_due)
        ):
            queued = _pool_queue(connection, self.tenant, intent.get("pool"))
            if not track.idle_noted:
                track.idle_noted = True
                self._entry(
                    key,
                    "idle",
                    "idle",
                    track.idle_since,
                    track.idle_since,
                    {"queued_in_pool": len(queued)},
                )
            if queued and probe_due:
                # The real claim gates against the first waiting jobs, read-only.
                track.probed_at = now
                if not track.refusals:
                    track.refusals_since = now
                for job in queued[:PROBE_JOBS]:
                    try:
                        reason = (
                            self.control.claim_reason(connection, worker, job)
                            if worker is not None
                            else "worker_not_registered"
                        )
                    except Exception as error:
                        reason = "check_failed_" + type(error).__name__
                    track.refusals["probe:" + (reason or "claimable_not_claimed")] += 1
                # The first probe of an idle episode is written at once.
                self._write_refusals(key, now, force=True, queued=len(queued))
        self._write_refusals(key, now)

    def _write_refusals(self, key, now, *, force=False, queued=None):
        track = self._tracks[key]
        if not track.refusals:
            return
        if not force and now - (track.refusals_since or now) < WINDOW_SECONDS:
            return
        detail = {
            "reasons": dict(track.refusals.most_common(20)),
            "window_s": round(now - (track.refusals_since or now), 1),
        }
        if queued is not None:
            detail["queued_in_pool"] = queued
        self._entry(key, "refusals", "claim_refused", now, None, detail)
        track.refusals, track.refusals_since = Counter(), None

    def _record_stop(self, key, node, payload, binding, claims, now):
        if node["desired_state"] != "stopped":
            return
        reason = (
            payload.get("stop_reason_code")
            or (payload.get("capacity_watchdog") or {}).get("reason")
            or binding.get("reason_code")
        )
        if self._changed(key, "stop", _code(reason)):
            self._write_refusals(key, now, force=True)
            self._entry(
                key,
                "stop",
                "stop_requested",
                node.get("updated_at") if _finite(node.get("updated_at")) else now,
                _code(reason),
                {
                    "reason": _code(reason),
                    "claims": claims.claims if claims else 0,
                    "submitted": claims.submitted if claims else 0,
                    "estimated_cost_microusd": binding.get("estimated_cost_microusd"),
                    "queued": self._queued,
                },
            )

    def _record_close(self, key, intent, binding, reserved, now):
        proof = binding.get("provider_removal_proof")
        if (
            isinstance(proof, dict)
            and proof.get("complete")
            and self._changed(key, "removal", True)
        ):
            self._entry(
                key,
                "removal",
                "removal_confirmed",
                proof.get("observed_at") if _finite(proof.get("observed_at")) else now,
                True,
                {"absent": proof.get("absent") is True},
            )
        if binding.get("no_allocation_proof") and self._changed(
            key, "no_allocation", True
        ):
            self._entry(key, "no_allocation", "no_allocation_confirmed", now, True, {})
        billing = [
            _code(binding.get("billing_state")),
            sorted({_code(row.state) for row in reserved if row.state}),
        ]
        if self._changed(key, "billing", billing):
            self._entry(
                key,
                "billing",
                "billing_" + (billing[0] or "unknown"),
                now,
                billing,
                {
                    "reservation_states": billing[1],
                    "settled": any(row.settled_at is not None for row in reserved),
                },
            )
        state = intent.get("state")
        shown = "serving" if state in ("ready", "busy") else state
        if isinstance(state, str) and self._changed(key, "intent", shown):
            self._entry(key, "intent", "intent_" + state, now, shown, {})
        settled = binding.get("billing_state") == "no_charge_confirmed" or any(
            row.settled_at is not None for row in reserved
        )
        stopped_at = binding.get("first_stopped_at")
        if state == "destroyed" and (
            settled or _finite(stopped_at) and now - stopped_at > CLOSE_AFTER_SECONDS
        ):
            self._write_refusals(key, now, force=True)
            self._entry(key, "closed", "timeline_closed", now, True, {})
            self._tracks[key].closed = True

    def _record_snapshots(self, key, binding, instance, now):
        track = self._tracks[key]
        if self.buffer is None:
            return
        items = self.buffer.take(binding.get("run_name"), binding.get("run_id"))
        if instance and binding.get("backend"):
            presence = inventory_presence(
                self.buffer.inventory(binding["backend"]), str(instance), now
            )
            if presence is not None:
                items.append((now, "inventory", presence))
        for at, kind, data in items:
            # Ages and accrued cost move every read; a change is anything else.
            compared = {
                name: item
                for name, item in data.items()
                if name not in ("observed_age_s", "cost", "last_processed_at")
            }
            digest = hashlib.sha256(
                json.dumps(compared, sort_keys=True, default=str).encode("utf-8")
            ).hexdigest()[:16]
            if (
                track.snapshot_hash.get(kind) == digest
                or track.snapshots >= MAX_SNAPSHOTS
            ):
                continue
            track.snapshot_hash[kind] = digest
            track.snapshots += 1
            self._writes.append(
                (
                    SNAPSHOT_EVENT,
                    key,
                    {
                        "version": 1,
                        "kind": kind,
                        "at": at,
                        "hash": digest,
                        "data": data,
                    },
                )
            )

    # Demand --------------------------------------------------------------
    def _record_demand(self, connection, result, now):
        key = demand_key(self.tenant)
        self._restore_demand(connection, key)
        track = self._tracks[key]
        state = result.get("state")
        if not isinstance(state, str):
            return
        reason, detail = (
            _code(result.get("reason_code")),
            _code(result.get("detail_code")),
        )
        queued = self._queued
        live = self._live
        previous = track.last.get("demand") or {}
        waiting = state not in WAITING and queued > 0
        stalled = waiting and live == 0
        blocked_since = previous.get("blocked_since") if waiting else None
        stalled_since = previous.get("stalled_since") if stalled else None
        value = {
            "state": state,
            "reason_code": reason,
            "detail_code": detail,
            "stalled": stalled,
            "blocked_since": blocked_since or (now if waiting else None),
            "stalled_since": stalled_since or (now if stalled else None),
        }
        if (
            track.window_start is not None
            and now - track.window_start >= WINDOW_SECONDS
        ):
            if track.suppressed:
                # Decisions over the burst limit, as exact codes with counts.
                self._entry(
                    key,
                    "demand",
                    "demand_summary",
                    now,
                    previous,
                    {**previous, "counts": dict(track.suppressed.most_common(20))},
                )
            track.window_start, track.window_writes = None, 0
            track.suppressed = Counter()
        compared = ("state", "reason_code", "detail_code", "stalled")
        if all(previous.get(name) == value[name] for name in compared):
            return
        if track.window_start is None:
            track.window_start = now
        flip = previous.get("stalled") != stalled
        if track.window_writes >= DEMAND_BURST and not flip:
            track.suppressed[detail or reason or state] += 1
            track.last["demand"] = value
            return
        track.window_writes += 1
        self._entry(
            key,
            "demand",
            reason or state,
            now,
            value,
            {
                **value,
                "queued": queued,
                "live_machines": live,
                "node_id": _code(result.get("node_id")),
            },
        )


def _pool_queue(connection, tenant, pool):
    """Queued work in the machine's pool, oldest first (diagnosis only)."""
    from .repository import jobs

    if not isinstance(pool, str):
        return []
    return [
        dict(row)
        for row in connection.execute(
            select(jobs)
            .where(
                jobs.c.tenant_id == tenant,
                jobs.c.pool == pool,
                jobs.c.status.in_(("queued", "waiting_capacity")),
            )
            .order_by(jobs.c.created_at)
            .limit(50)
        ).mappings()
    ]


def _queued(connection, tenant):
    from .repository import jobs

    return connection.execute(
        select(func.count())
        .select_from(jobs)
        .where(jobs.c.tenant_id == tenant, jobs.c.status == "queued")
    ).scalar()


def _worker_id(intent_id):
    return "dstack-" + intent_id.replace("-", "")
