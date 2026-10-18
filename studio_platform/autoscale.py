"""Side-effect-free capacity planning. Defaults forbid creating cloud resources.

Predictions use execution slots, not physical GPU count. A TP2 worker is one slot
unless separately validated. Starting slots are future capacity. Unknown instances
still consume the approved instance/GPU limits and budget.
"""

from __future__ import annotations

from dataclasses import dataclass
import heapq
import math


@dataclass(frozen=True)
class Demand:
    job_id: str
    owner_id: str
    created_at: float
    runtime_s: float
    confidence: str = "observed"


@dataclass(frozen=True)
class Slot:
    worker_id: str
    state: str = "ready"
    available_after_s: float = 0


@dataclass(frozen=True)
class ScalePolicy:
    dry_run: bool = True
    max_instances: int = 0
    max_physical_gpus: int = 0
    queue_target_s: float = 300
    min_improvement_s: float = 120
    cooldown_s: float = 300
    cold_start_s: float = 600
    new_instance_slots: int = 1
    new_instance_physical_gpus: int = 1
    idle_before_drain_s: float = 900
    # Budget for boot, running, idle, uncertain states and contingency must be
    # supplied explicitly. None is unknown, zero is no approved spending.
    approved_remaining_microusd: int | None = None
    instance_reservation_microusd: int | None = None
    hard_deadline: float | None = None


@dataclass(frozen=True)
class ScaleState:
    consecutive_breaches: int = 0
    last_scale_at: float | None = None


@dataclass(frozen=True)
class Recommendation:
    action: str
    reason: str
    predicted_max_wait_s: float | None
    predicted_finish_s: float | None
    improvement_s: float | None
    state: ScaleState


def runtime_estimate(samples):
    """No comparable samples means unknown, not an invented latency promise."""
    samples = sorted(float(s) for s in samples)
    if any(not math.isfinite(s) or s <= 0 for s in samples):
        raise ValueError("invalid_runtime_sample")
    if not samples:
        return {"runtime_s": None, "confidence": "unknown", "samples": 0}
    if len(samples) < 5:
        return {
            "runtime_s": max(samples) * 1.25,
            "confidence": "low",
            "samples": len(samples),
        }
    return {
        "runtime_s": samples[math.ceil(len(samples) * 0.9) - 1],
        "confidence": "observed",
        "samples": len(samples),
    }


def predict(demands, slots, *, now):
    """Return a conservative FIFO makespan/oldest wait simulation; no dispatch."""
    timeline = []
    for i, slot in enumerate(slots):
        if slot.state in ("ready", "busy", "starting"):
            if not math.isfinite(slot.available_after_s) or slot.available_after_s < 0:
                raise ValueError("invalid_slot_availability")
            heapq.heappush(timeline, (float(slot.available_after_s), i))
    if not demands:
        return {"max_wait_s": 0.0, "finish_s": 0.0, "starts": {}}
    if not timeline:
        return {"max_wait_s": None, "finish_s": None, "starts": {}}
    max_wait, finish, starts = 0.0, 0.0, {}
    for demand in sorted(demands, key=lambda d: (d.created_at, d.job_id)):
        if not math.isfinite(demand.runtime_s) or demand.runtime_s <= 0:
            raise ValueError("invalid_demand_runtime")
        available, slot_i = heapq.heappop(timeline)
        starts[demand.job_id] = available
        max_wait = max(max_wait, max(0, now - demand.created_at) + available)
        finish = max(finish, available + demand.runtime_s)
        heapq.heappush(timeline, (available + demand.runtime_s, slot_i))
    return {"max_wait_s": max_wait, "finish_s": finish, "starts": starts}


def recommend(
    demands, slots, instance_records, *, now, policy=ScalePolicy(), state=ScaleState()
):
    """Recommend at most ONE instance. This function cannot rent anything.

    Caller persists observations and then atomically reserves an instance intent
    using Repository. Proposal is not permission, and prediction is not an SLO.
    """
    times = (
        now,
        policy.cold_start_s,
        policy.cooldown_s,
        policy.queue_target_s,
        policy.min_improvement_s,
        policy.idle_before_drain_s,
    )
    counts = (
        policy.new_instance_slots,
        policy.new_instance_physical_gpus,
        policy.max_instances,
        policy.max_physical_gpus,
    )
    budgets = (policy.approved_remaining_microusd, policy.instance_reservation_microusd)
    if (
        any(not isinstance(v, (int, float)) or not math.isfinite(v) for v in times)
        or any(v < 0 for v in times[1:])
        or any(type(v) is not int or v < 0 for v in counts)
        or policy.new_instance_slots < 1
        or policy.new_instance_physical_gpus < 1
        or any(v is not None and (type(v) is not int or v < 0) for v in budgets)
        or policy.hard_deadline is not None
        and not math.isfinite(policy.hard_deadline)
    ):
        raise ValueError("invalid_scale_policy")
    if any(d.confidence == "unknown" for d in demands):
        return Recommendation(
            "none",
            "runtime_unknown",
            None,
            None,
            None,
            ScaleState(0, state.last_scale_at),
        )
    baseline = predict(demands, slots, now=now)
    breach = bool(demands) and (
        baseline["max_wait_s"] is None or baseline["max_wait_s"] > policy.queue_target_s
    )
    new_state = ScaleState(
        state.consecutive_breaches + 1 if breach else 0, state.last_scale_at
    )

    def result(action, reason, improvement=None):
        return Recommendation(
            action,
            reason,
            baseline["max_wait_s"],
            baseline["finish_s"],
            improvement,
            new_state,
        )

    if not breach:
        return result("none", "capacity_sufficient")
    if new_state.consecutive_breaches < 2:
        return result("none", "observe_again")
    if (
        state.last_scale_at is not None
        and now - state.last_scale_at < policy.cooldown_s
    ):
        return result("none", "cooldown")
    additional = [
        Slot("proposal-" + str(i), "starting", policy.cold_start_s)
        for i in range(policy.new_instance_slots)
    ]
    proposed = predict(demands, [*slots, *additional], now=now)
    improvement = (
        None
        if baseline["finish_s"] is None
        else baseline["finish_s"] - proposed["finish_s"]
    )
    if improvement is not None and improvement < policy.min_improvement_s:
        return result("none", "cold_start_no_benefit", improvement)
    active = [r for r in instance_records if r.get("state") != "destroyed"]
    if (
        len(active) + 1 > policy.max_instances
        or sum(r["physical_gpus"] for r in active) + policy.new_instance_physical_gpus
        > policy.max_physical_gpus
    ):
        return result("none", "capacity_limit", improvement)
    if (
        policy.approved_remaining_microusd is None
        or policy.instance_reservation_microusd is None
        or policy.instance_reservation_microusd <= 0
        or policy.instance_reservation_microusd > policy.approved_remaining_microusd
    ):
        return result("none", "budget_not_approved", improvement)
    if (
        policy.hard_deadline is None
        or now + proposed["finish_s"] > policy.hard_deadline
    ):
        return result("none", "deadline_not_safe", improvement)
    return result(
        "dry_run" if policy.dry_run else "propose", "scale_one_instance", improvement
    )


def may_drain(
    instance,
    *,
    now,
    active_job_count,
    unresolved_attempt_count,
    collection_count,
    policy=ScalePolicy(),
):
    """A drain recommendation never destroys a host or confirms stopped billing."""
    if instance.get("state") != "ready" or instance.get("idle_since") is None:
        return False
    return (
        active_job_count == 0
        and unresolved_attempt_count == 0
        and collection_count == 0
        and now - instance["idle_since"] >= policy.idle_before_drain_s
    )
