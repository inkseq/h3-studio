"""CR76/77 simulation only: no queue, database, provider, or admission authority.

All durations include delivery and come from a caller's qualified measurements.
Uncertain occupancy remains charged and blocks additional proposed starts.
"""

from collections import defaultdict
from dataclasses import dataclass
import math


def number(value, *, positive=False, integer=False):
    if (
        type(value) not in (int, float)
        or not math.isfinite(value)
        or value < 0
        or positive
        and value == 0
        or value > 1_000_000_000_000
        or integer
        and type(value) is not int
    ):
        raise ValueError("capacity_wait_input_invalid")


def identity(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 256:
        raise ValueError("capacity_wait_identity_invalid")


@dataclass(frozen=True)
class Shot:
    id: str
    owner: str
    group: str
    queued_seconds: float
    # Per-backend qualified seconds, including upload/delivery. No default 40s.
    durations: tuple[tuple[str, float], ...]
    serial_key: str | None = None

    def __post_init__(self):
        for value in (self.id, self.owner, self.group):
            identity(value)
        number(self.queued_seconds)
        if self.serial_key is not None:
            identity(self.serial_key)
        if len(dict(self.durations)) != len(self.durations):
            raise ValueError("capacity_wait_duplicate_duration")
        for backend, seconds in self.durations:
            if backend not in {"modal", "dedicated"}:
                raise ValueError("capacity_wait_backend_invalid")
            number(seconds, positive=True)


@dataclass(frozen=True)
class Slot:
    id: str
    backend: str
    groups: tuple[str, ...]
    state: str
    hourly_microusd: int | None
    # Includes remaining original work; None supplies no predicted throughput.
    ready_seconds: float | None
    # A currently running original's delivery contributes once to its owner.
    completing_owner: str | None = None
    serial_key: str | None = None

    def __post_init__(self):
        identity(self.id)
        if self.backend not in {"modal", "dedicated"} or self.state not in {
            "ready",
            "starting",
            "running",
            "stopping",
            "unknown",
        }:
            raise ValueError("capacity_wait_slot_invalid")
        if len(set(self.groups)) != len(self.groups):
            raise ValueError("capacity_wait_duplicate_group")
        for group in self.groups:
            identity(group)
        if self.hourly_microusd is not None:
            number(self.hourly_microusd, positive=True, integer=True)
        if self.ready_seconds is not None:
            number(self.ready_seconds)
        if self.state == "ready" and self.ready_seconds != 0:
            raise ValueError("capacity_wait_ready_invalid")
        if self.completing_owner is not None:
            identity(self.completing_owner)
            if self.state != "running" or self.ready_seconds is None:
                raise ValueError("capacity_wait_completion_invalid")
        if self.serial_key is not None:
            identity(self.serial_key)
            if self.completing_owner is None:
                raise ValueError("capacity_wait_serial_completion_required")


@dataclass(frozen=True)
class Targets:
    first_seconds: int
    interval_seconds: int
    trigger_seconds: int
    hourly_limit_microusd: int
    machine_limit: int

    def __post_init__(self):
        for value in (
            self.first_seconds,
            self.interval_seconds,
            self.trigger_seconds,
            self.hourly_limit_microusd,
            self.machine_limit,
        ):
            number(value, positive=True, integer=True)
        if (
            self.trigger_seconds > self.first_seconds
            or self.hourly_limit_microusd > 20_000_000
            or self.machine_limit > 64
        ):
            raise ValueError("capacity_wait_targets_invalid")


@dataclass(frozen=True)
class ModalOption:
    group: str
    boot_seconds: float
    hourly_microusd: int
    # Required receipt identifier, not a boolean claiming an assumed time works.
    qualification: str | None = None

    def __post_init__(self):
        identity(self.group)
        number(self.boot_seconds)
        number(self.hourly_microusd, positive=True, integer=True)
        if self.qualification is not None:
            identity(self.qualification)


def fair_order(shots, slots):
    groups, active = defaultdict(list), defaultdict(int)
    for slot in slots:
        if slot.completing_owner is not None:
            active[slot.completing_owner] += 1
    for shot in sorted(shots, key=lambda s: (-s.queued_seconds, s.id)):
        groups[shot.owner].append(shot)
    # Each owner's first pending shot precedes later shots, then per-owner FIFO.
    ranked = [
        (depth, active[owner], -shot.queued_seconds, owner, shot.id, shot)
        for owner, owned in groups.items()
        for depth, shot in enumerate(owned)
    ]
    return [row[-1] for row in sorted(ranked)]


def simulate(shots, slots, targets, last_delivery, episode_seconds):
    """Greedy finite simulation; prefer dedicated when slots are equally ready."""
    available = {
        slot.id: slot.ready_seconds
        for slot in slots
        if slot.state in {"ready", "starting", "running"}
        and slot.ready_seconds is not None
    }
    delivered, missed, ages = defaultdict(list), defaultdict(int), {}
    serialized = {}
    for slot in slots:
        if slot.completing_owner is not None:
            delivered[slot.completing_owner].append(slot.ready_seconds)
        if slot.serial_key is not None:
            serialized[slot.serial_key] = max(
                serialized.get(slot.serial_key, 0), slot.ready_seconds
            )
    for shot in fair_order(shots, slots):
        ages[shot.owner] = max(
            ages.get(shot.owner, 0),
            shot.queued_seconds,
            episode_seconds.get(shot.owner, 0),
        )
        durations = dict(shot.durations)
        options = [
            s
            for s in slots
            if s.id in available and shot.group in s.groups and s.backend in durations
        ]
        if not options:
            missed[shot.owner] += 1
            continue
        not_before = serialized.get(shot.serial_key, 0)
        chosen = min(
            options,
            key=lambda s: (
                max(available[s.id], not_before),
                s.backend != "dedicated",
                s.id,
            ),
        )
        finish = max(available[chosen.id], not_before) + durations[chosen.backend]
        if not math.isfinite(finish):
            raise ValueError("capacity_wait_duration_overflow")
        available[chosen.id] = finish
        if shot.serial_key is not None:
            serialized[shot.serial_key] = finish
        delivered[shot.owner].append(finish)
    result = []
    for owner in sorted(ages):
        times = sorted(delivered[owner])
        previous = last_delivery.get(owner)
        first_wait = times[0] + ages[owner] if times and previous is None else None
        gaps = [b - a for a, b in zip(times, times[1:], strict=False)]
        if previous is not None and times:
            gaps.append(previous + times[0])
        interval = max(gaps, default=0) if times else None
        first_ok = previous is not None or (
            first_wait is not None and first_wait <= targets.first_seconds
        )
        result.append(
            {
                "owner": owner,
                "first_video_eta_seconds": times[0] if times else None,
                "first_video_wait_seconds": first_wait,
                "maximum_video_interval_seconds": interval,
                "unplaced_shots": missed[owner],
                "first_target_met": first_ok and not missed[owner],
                "interval_target_met": interval is not None
                and interval <= targets.interval_seconds
                and not missed[owner],
            }
        )
    return result


def score(rows, targets):
    overdue = [
        max(0, (row["first_video_wait_seconds"] or 0) - targets.trigger_seconds)
        + max(
            0, (row["maximum_video_interval_seconds"] or 0) - targets.interval_seconds
        )
        for row in rows
    ]
    return (
        sum(r["unplaced_shots"] for r in rows),
        sum(
            not (r["first_target_met"] and r["interval_target_met"])
            or (r["first_video_wait_seconds"] or 0) > targets.trigger_seconds
            for r in rows
        ),
        max(overdue, default=0),
        sum(overdue),
    )


def plan(
    shots,
    slots,
    targets,
    option,
    *,
    last_delivery=None,
    episode_seconds=None,
    admission_blocked=True,
):
    """Return an advisory proposal. Every original admission must still recheck.

    slots MUST include all global paid obligations, even incompatible hosts.
    last_delivery maps owners to seconds since their last finished video in
    the current continuously queued episode; absent means no completion yet.
    No historical/offline time should be supplied as an active episode gap.
    """
    shots, slots = tuple(shots), tuple(slots)
    last_delivery = dict(last_delivery or {})
    episode_seconds = dict(episode_seconds or {})
    if len(shots) > 10_000 or len(slots) > 256:
        raise ValueError("capacity_wait_snapshot_too_large")
    if len({s.id for s in shots}) != len(shots) or len({s.id for s in slots}) != len(
        slots
    ):
        raise ValueError("capacity_wait_duplicate_identity")
    if type(admission_blocked) is not bool:
        raise ValueError("capacity_wait_admission_invalid")
    for owner, age in (*last_delivery.items(), *episode_seconds.items()):
        identity(owner)
        number(age)
    queued_owners = {s.owner for s in shots}
    if any(
        s.completing_owner in queued_owners
        and s.completing_owner not in last_delivery
        and s.completing_owner not in episode_seconds
        for s in slots
    ):
        raise ValueError("capacity_wait_episode_required")
    before = simulate(shots, slots, targets, last_delivery, episode_seconds)
    unknown = any(
        s.hourly_microusd is None
        or s.state == "unknown"
        or s.state in {"starting", "running"}
        and s.ready_seconds is None
        for s in slots
    )
    hourly = (
        None
        if any(s.hourly_microusd is None for s in slots)
        else sum(s.hourly_microusd for s in slots)
    )
    headroom = (
        0
        if unknown
        else min(
            max(0, targets.machine_limit - len(slots)),
            max(0, targets.hourly_limit_microusd - hourly) // option.hourly_microusd,
            sum(s.group == option.group for s in shots),
        )
    )
    trigger = any(
        r["first_video_eta_seconds"] is None
        or r["first_video_wait_seconds"] is not None
        and r["first_video_wait_seconds"] > targets.trigger_seconds
        or not r["interval_target_met"]
        for r in before
    )
    reason = (
        "no_demand"
        if not shots
        else "covered"
        if not trigger
        else "admission_blocked"
        if admission_blocked
        else "capacity_unconfirmed"
        if unknown
        else "timing_unqualified"
        if option.qualification is None
        else "capacity_limit"
        if not headroom
        else "target_unreachable"
    )
    best, count, best_score = before, 0, score(before, targets)
    if reason == "target_unreachable":
        for proposed in range(1, headroom + 1):
            extras = tuple(
                Slot(
                    "proposal:" + str(i),
                    "modal",
                    (option.group,),
                    "starting",
                    option.hourly_microusd,
                    option.boot_seconds,
                )
                for i in range(proposed)
            )
            if any(s.id.startswith("proposal:") for s in slots):
                raise ValueError("capacity_wait_reserved_identity")
            projected = simulate(
                shots, slots + extras, targets, last_delivery, episode_seconds
            )
            value = score(projected, targets)
            if value < best_score:
                best, count, best_score = projected, proposed, value
            if value[:2] == (0, 0):
                reason = "targets_projected"
                break
        if reason == "target_unreachable" and count == headroom:
            reason = "capacity_limit"
    return {
        "version": 1,
        "advisory_only": True,
        "reason": reason,
        "qualification": option.qualification,
        "additional_modal": count,
        "occupied_machines": len(slots),
        "committed_hourly_microusd": hourly,
        "proposed_hourly_microusd": count * option.hourly_microusd,
        "before": before,
        "projected": best,
    }
