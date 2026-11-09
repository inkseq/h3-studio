"""Observability check for the controller simulator.

The simulator knows the ground truth: which machine idles with work waiting,
whose worker vanished, whose download stalled and why demand cannot start.
For every such problem this check requires persisted, operator-visible
evidence carrying the exact reason within a bounded time: an outbox event (a
capacity timeline entry, a provider snapshot, a stop or reclaim event) or a
capacity health alert. A log line is not evidence; it is gone within hours on
staging (docker json-file, 5 MB x 2).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select

from studio_platform.repository import outbox

# Seconds a problem may stay without evidence once it is detected.
BOUND_SECONDS = 600
# A problem is "detected" after it lasted this long (one slow tick is not one).
DETECT_SECONDS = 300
TYPES = (
    "capacity.timeline",
    "capacity.provider_snapshot",
    "capacity.health_alert",
    "capacity.node_replacement_requested",
    "capacity.attempt_lost",
    "dstack.idle_drain_requested",
    "dstack.provider_removal_confirmed",
    "dstack.no_allocation_confirmed",
    "dstack.demand_safety_paused",
)


@dataclass
class Expectation:
    kind: str
    aggregates: tuple
    since: float
    deadline: float
    accept: object  # callable(event_type, payload) -> bool
    detail: str = ""
    found: bool = field(default=False)


def timeline(*codes, stages=()):
    """A timeline entry with one of these exact codes (or in these stages)."""

    def accept(kind, payload):
        return kind == "capacity.timeline" and (
            payload.get("code") in codes or payload.get("stage") in stages
        )

    return accept


def alert(code, *diagnoses):
    def accept(kind, payload):
        return (
            kind == "capacity.health_alert"
            and payload.get("code") == code
            and (not diagnoses or payload.get("diagnosis") in diagnoses)
        )

    return accept


def stop_with(*codes):
    """A stop request (timeline, drain or watchdog event) with one of these reasons."""

    def accept(kind, payload):
        if kind == "capacity.timeline":
            return payload.get("code") == "stop_requested" and (
                payload.get("detail", {}).get("reason") in codes
            )
        return (
            kind
            in ("dstack.idle_drain_requested", "capacity.node_replacement_requested")
            and payload.get("reason_code") in codes
        )

    return accept


def any_of(*accepts):
    return lambda kind, payload: any(item(kind, payload) for item in accepts)


class Evidence:
    """Open expectations, checked against the outbox after every tick."""

    def __init__(self, world):
        self.world = world
        self.open = {}
        self.done = set()
        self.gaps = []
        self.queued_since = {}

    def expect(
        self, key, kind, aggregates, since, accept, *, bound=BOUND_SECONDS, detail=""
    ):
        if key in self.open or key in self.done:
            return
        self.open[key] = Expectation(
            kind, tuple(aggregates), since, self.world.now + bound, accept, detail
        )

    def check(self, *, final=False):
        world = self.world
        if not self.open:
            return
        aggregates = sorted({a for item in self.open.values() for a in item.aggregates})
        earliest = min(item.since for item in self.open.values()) - 120
        with world.repo.engine.connect() as connection:
            rows = list(
                connection.execute(
                    select(
                        outbox.c.event_type,
                        outbox.c.aggregate_id,
                        outbox.c.payload,
                        outbox.c.created_at,
                    ).where(
                        outbox.c.event_type.in_(TYPES),
                        outbox.c.aggregate_id.in_(aggregates),
                        outbox.c.created_at >= earliest,
                    )
                ).mappings()
            )
        for key, item in list(self.open.items()):
            hit = any(
                row["aggregate_id"] in item.aggregates
                and row["created_at"] >= item.since - 120
                and item.accept(row["event_type"], row["payload"] or {})
                for row in rows
            )
            if hit:
                world.reasons["evidence:" + item.kind] += 1
            elif world.now > item.deadline or final:
                if final and world.now <= item.deadline:
                    # The run ended before the bound; nothing to judge.
                    world.reasons["evidence_unjudged:" + item.kind] += 1
                    self.done.add(key)
                    del self.open[key]
                    continue
                gap = "%s %s" % (item.kind, item.detail)
                self.gaps.append(gap)
                world.violate("unobserved_" + item.kind, item.detail)
            else:
                continue
            self.done.add(key)
            del self.open[key]

    # Ground-truth detectors -------------------------------------------------
    def detect(self, queued, live):
        """Called by the simulator after each tick with its own truth."""
        world, now = self.world, self.world.now
        demand = "demand:" + world.scope.tenant_id
        for machine in live:
            key = machine.intent_id
            short = key[:8]
            phase = (
                machine.phase(now)[0] if machine.allocated_at() is not None else None
            )
            # A booted machine idles while it could still take queued work.
            if machine.idle_with_work >= DETECT_SECONDS:
                since = now - machine.idle_with_work
                self.expect(
                    ("idle", key, round(since)),
                    "paid_idle_with_queue",
                    (key,),
                    since,
                    any_of(
                        timeline("claim_refused"),
                        alert("capacity_paid_idle_queue"),
                        lambda kind, payload: (
                            kind == "capacity.timeline"
                            and payload.get("code") == "stop_requested"
                        ),
                    ),
                    detail="%s idle %ds with queued work"
                    % (short, machine.idle_with_work),
                )
            # The worker is gone while the machine still bills.
            if (
                phase == "ready"
                and machine.stop_requested_at is None
                and not machine.worker_alive(world)
            ):
                lost = machine.lost_since(world)
                self.expect(
                    ("worker", key),
                    "lost_worker",
                    (key,),
                    lost,
                    any_of(
                        timeline("worker_expired", "worker_unknown"),
                        # The runtime read through the lost worker's tunnel fails.
                        lambda kind, payload: (
                            kind == "capacity.timeline"
                            and payload.get("stage") == "boot"
                            and payload.get("detail", {}).get("state")
                            not in ("ready", "busy", None)
                        ),
                        alert("capacity_paid_idle_queue", "worker_unreachable"),
                        stop_with("worker_unreachable", "capacity_worker_lost"),
                    ),
                    detail="%s worker gone at t=%dm"
                    % (short, (lost - world.start) // 60),
                )
            # A stalled or too-slow model download.
            stalled = machine.download_problem(now)
            if stalled is not None:
                since, what = stalled
                if now - since >= 0:
                    self.expect(
                        ("download", key),
                        "slow_download",
                        (key,),
                        since,
                        any_of(
                            timeline("download_stalled", "download_slow"),
                            alert("capacity_download_overdue"),
                            stop_with(
                                "dstack_download_too_slow",
                                "dstack_download_timebox_exceeded",
                            ),
                        ),
                        detail="%s download %s" % (short, what),
                    )
        # Queued work with no machine at all.
        pending = [
            m
            for m in world.client.machines.values()
            if m.gone_at is None and not m.script.no_capacity
        ]
        if queued and not pending and world.demand_switch:
            world._evidence_blocked_since = (
                getattr(world, "_evidence_blocked_since", None) or now
            )
            blocked = now - world._evidence_blocked_since
            since = world._evidence_blocked_since
            if blocked >= DETECT_SECONDS:
                self.expect(
                    ("blocked", round(since)),
                    "start_blocked_no_machine",
                    (demand,),
                    since,
                    lambda kind, payload: (
                        kind == "capacity.timeline"
                        and payload.get("stage") == "demand"
                        and payload.get("detail", {}).get("state")
                        not in ("accepted", None)
                        and bool(payload.get("code"))
                    ),
                    detail="%d queued, no machine, demand says %s"
                    % (len(queued), world._demand_reason),
                )
            if blocked >= 1800:
                self.expect(
                    ("blocked_alert", round(since)),
                    "start_blocked_alert",
                    (demand,),
                    since,
                    lambda kind, payload: (
                        kind == "capacity.health_alert"
                        and payload.get("code") == "capacity_start_blocked"
                        and bool(payload.get("detail"))
                    ),
                    detail="blocked %dm, demand says %s"
                    % (blocked // 60, world._demand_reason),
                )
        else:
            world._evidence_blocked_since = None
        # Jobs held by a machine the provider removed.
        for job_id, machine in world._orphans():
            self.expect(
                ("orphan", job_id),
                "stuck_job",
                (job_id, machine.intent_id),
                machine.gone_at,
                lambda kind, payload: (
                    kind
                    in ("capacity.attempt_lost", "dstack.provider_removal_confirmed")
                    or kind == "capacity.timeline"
                    and payload.get("code") in ("removal_confirmed", "run_failed")
                ),
                bound=900,
                detail="%s held by removed %s" % (job_id[:8], machine.intent_id[:8]),
            )
        # Work queued without a break for longer than the health window (plus
        # one health interval and a tick, so work claimed just in time is not
        # held against it). A requeue after a lost attempt starts a new wait.
        waiting = {job["id"] for job in queued}
        for job_id in list(self.queued_since):
            if job_id not in waiting:
                del self.queued_since[job_id]
        for job in queued:
            since = self.queued_since.setdefault(job["id"], now)
            age = now - since
            if age >= 1800 + 240:
                self.expect(
                    ("queue_age", job["id"], round(since)),
                    "stuck_job",
                    (job["id"],),
                    since,
                    alert("capacity_queue_age"),
                    detail="%s queued %dm" % (job["id"][:8], age // 60),
                )

    def demand_refused(self, reason, detail):
        """A blocked demand decision: its exact code must be on the timeline."""
        world = self.world
        demand = "demand:" + world.scope.tenant_id
        exact = detail or reason
        self.expect(
            ("demand", reason, detail),
            "refused_start",
            (demand,),
            world.now - 1,
            lambda kind, payload: (
                kind == "capacity.timeline"
                and payload.get("stage") == "demand"
                and (
                    payload.get("code") == reason
                    and payload.get("detail", {}).get("detail_code") == detail
                    or exact in payload.get("detail", {}).get("counts", {})
                )
            ),
            detail="demand %s/%s" % (reason, detail),
        )
        if reason == "budget_limit" or "limit" in (detail or ""):
            self.expect(
                ("budget", detail or reason),
                "budget_block",
                (demand,),
                world.now - 1,
                lambda kind, payload: (
                    kind == "capacity.timeline"
                    and payload.get("stage") == "demand"
                    and payload.get("detail", {}).get("detail_code") == detail
                ),
                detail="demand %s/%s" % (reason, detail),
            )

    def create_failed(self, machine, code):
        """A create call that failed or found no capacity: the node says so."""
        self.expect(
            ("create", machine.intent_id),
            "refused_start",
            (machine.intent_id,),
            self.world.now - 1,
            lambda kind, payload: (
                kind == "capacity.provider_snapshot"
                and payload.get("kind") == "create"
                and payload.get("data", {}).get("error") == code
                or kind == "capacity.timeline"
                and payload.get("code") in ("no_allocation_confirmed", "run_failed")
                or kind == "dstack.no_allocation_confirmed"
            ),
            detail="%s create %s" % (machine.intent_id[:8], code),
        )

    def deferred(self, machine, reason):
        """A claimed job whose submission was deferred: the reason is stored."""
        self.expect(
            ("defer", machine.intent_id, reason),
            "refused_claim",
            (machine.intent_id,),
            self.world.now - 1,
            lambda kind, payload: (
                kind == "capacity.timeline"
                and payload.get("code") == "claim_refused"
                and "defer:" + reason in payload.get("detail", {}).get("reasons", {})
            ),
            detail="%s submission deferred: %s" % (machine.intent_id[:8], reason),
        )
