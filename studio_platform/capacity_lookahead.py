"""Pure per-pool planning for #688; not connected to provider admission.

A decision is a snapshot, never a reservation or authority to start/stop.
The execution integration must recheck original journals and all hard gates.
"""

from dataclasses import dataclass
from math import floor, isfinite


def _number(value, *, positive=False, integer=False):
    if (
        type(value) not in (int, float)
        or not isfinite(value)
        or value < 0
        or (positive and value == 0)
        or (integer and type(value) is not int)
    ):
        raise ValueError("capacity_lookahead_invalid_input")


@dataclass(frozen=True)
class Policy:
    job_seconds: float = 30
    boot_seconds: float = 600
    minimum_jobs: int = 20
    max_machines: int = 10

    def __post_init__(self):
        _number(self.job_seconds, positive=True)
        _number(self.boot_seconds, positive=True)
        _number(self.minimum_jobs, positive=True, integer=True)
        _number(self.max_machines, integer=True)


@dataclass(frozen=True)
class Machine:
    """Only compatible, owned capacity; ready means ready to claim work.

    A busy ready machine's outstanding work belongs in remaining_job_seconds.
    Missing startup ETA is conservative: counted as starting, no throughput.
    Allocation-unknown capacity belongs in the admission blocker, not here.
    """

    identity: str
    state: str
    ready_in_seconds: float | None = None
    idle_seconds: float | None = None

    def __post_init__(self):
        if not isinstance(self.identity, str) or not self.identity.strip():
            raise ValueError("capacity_lookahead_invalid_machine")
        if self.state not in {"ready", "starting"}:
            raise ValueError("capacity_lookahead_invalid_machine")
        for value in (self.ready_in_seconds, self.idle_seconds):
            if value is not None:
                _number(value)
        if self.state == "ready" and self.ready_in_seconds not in (None, 0):
            raise ValueError("capacity_lookahead_invalid_machine")
        if self.state == "starting" and self.idle_seconds is not None:
            raise ValueError("capacity_lookahead_invalid_machine")


@dataclass(frozen=True)
class Decision:
    left_at_ready: float
    start_count: int
    idle_candidates: tuple[str, ...]
    reason: str


def plan(
    policy: Policy,
    machines: tuple[Machine, ...],
    *,
    queued: int,
    remaining_job_seconds: float = 0,
    expected_arrivals: float = 0,
    available_slots: int,
    admission_blocked: bool,
) -> Decision:
    """Plan dedicated capacity; no guessed arrivals or provider prices.

    available_slots is the minimum affordable count under all global/hourly/
    task limits, including incompatible capacity. Unknown headroom must be zero.
    admission_blocked includes disabled policy, safety pause and uncertain creates.
    Idle candidates require a separate fresh no-lease/no-queue stop guard.
    """
    _number(queued, integer=True)
    _number(remaining_job_seconds)
    _number(expected_arrivals)
    _number(available_slots, integer=True)
    if type(admission_blocked) is not bool:
        raise ValueError("capacity_lookahead_invalid_input")
    if len({machine.identity for machine in machines}) != len(machines):
        raise ValueError("capacity_lookahead_duplicate_machine")
    processed = sum(
        policy.boot_seconds / policy.job_seconds
        if machine.state == "ready"
        else max(0, policy.boot_seconds - machine.ready_in_seconds) / policy.job_seconds
        if machine.ready_in_seconds is not None
        else 0
        for machine in machines
    )
    left = max(
        0,
        queued
        + remaining_job_seconds / policy.job_seconds
        + expected_arrivals
        - processed,
    )
    if not isfinite(left):
        raise ValueError("capacity_lookahead_invalid_input")
    idle = tuple(
        sorted(
            machine.identity
            for machine in machines
            if queued == 0
            and remaining_job_seconds == 0
            and machine.state == "ready"
            and machine.idle_seconds is not None
            and machine.idle_seconds >= policy.boot_seconds
        )
    )
    # Forecast alone cannot rent; a cold pool with actual demand gets one floor.
    wanted = (
        max(1, floor(left / policy.minimum_jobs))
        if not machines and queued
        else (floor(left / policy.minimum_jobs) if queued else 0)
    )
    slots = min(available_slots, max(0, policy.max_machines - len(machines)))
    count = min(wanted, slots) if not admission_blocked else 0
    reason = (
        "admission_blocked"
        if admission_blocked and wanted
        else "capacity_limit"
        if wanted and not slots
        else "floor"
        if count and not machines
        else "lookahead"
        if count
        else "no_demand"
        if not queued
        else "covered"
    )
    return Decision(left, count, idle, reason)
