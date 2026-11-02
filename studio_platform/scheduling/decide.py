"""Which queued jobs still need a new machine this tick (#931 step 3).

Pure: no database, provider or clock. The controller reads the snapshot
(queued work, jobs a live Modal host covers, Modal hosts as lookahead
capacity) and applies the result through the original demand-start journal,
budget and capacity checks, which stay where they are.

Today's rules, unchanged and table-tested:
- one provider per job (#784): a job a live Modal host was started for is
  covered and never also gets a dstack start;
- Modal first (#688, owner 2026-10-29), when `Rules.vast_min_queue` is set:
  dstack rents only while the boot-time lookahead still sees that many jobs
  left once a new machine would be ready, or for a job that has waited
  `vast_after_seconds` (Modal refused or failed it).

SCENARIOS maps the eleven documented cases to their decision owners. Hosted
predicates are applied at the original call sites, not as a global route
selector: several cases may apply at once. Claims, money, provider operations
and idle stopping remain with their transactional owners.
"""

from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class Rules:
    vast_min_queue: int | None = None
    vast_after_seconds: int | None = None

    @classmethod
    def from_modal_first(cls, value):
        """The controller's `modal_first` dict (modal.json), or no rule."""
        if not value:
            return cls()
        return cls(value["vast_min_queue"], value["vast_after_seconds"])


def vast_share(rules, queued, work, now, machines):
    """Work dstack may start for while Modal hosts are the floor."""
    from ..capacity_lookahead import Policy, plan

    decision = plan(
        Policy(minimum_jobs=rules.vast_min_queue),
        machines,
        queued=len(queued),
        available_slots=1,
        admission_blocked=False,
    )
    if decision.start_count:
        return work
    return [
        item for item in work if now - item[0]["created_at"] >= rules.vast_after_seconds
    ]


def dstack_work(queued, covered, rules, machines, now):
    """(job, profile) pairs that still need a dstack start, in queue order."""
    work = [item for item in queued if item[0]["id"] not in covered]
    if rules.vast_min_queue is not None:
        work = vast_share(rules, queued, work, now, machines)
    return work


# Seconds a job may wait for a machine of ours before going hosted (#918).
WAIT_LIMITS = {"fast": 30, "standard": 60, "refine": 120, "economy": 0}
# The vendor allows 40 renders per 15 min per account, shared by every
# environment; each environment's share comes from its routing file.
HOSTED_WINDOW_SECONDS = 900
HOSTED_PER_OWNER = 10


def _wait_elapsed(job, tier, enabled_tiers, now):
    limit = WAIT_LIMITS.get(tier)
    return (
        tier in enabled_tiers
        and limit is not None
        and not now - job["created_at"] < limit
    )


def hosted_offload(queued, tier_of, enabled_tiers, used, now, *, per_window):
    """Jobs to hand to the hosted API now, oldest first.

    A job goes hosted once it has waited past its tier's limit: a free machine
    of ours would have claimed it by then. `used` is {owner_id: renders in the
    current window}; the window and per-owner shares bound what is returned.
    """
    total = sum(used.values())
    owners = dict(used)
    chosen = []
    for job, profile in sorted(queued, key=lambda item: item[0]["created_at"]):
        tier = tier_of(job)
        if not SCENARIOS["occasional_click"].offload(
            job, tier, enabled_tiers, now
        ) or not SCENARIOS["window_full"].offload(total, owners, job, per_window):
            continue
        chosen.append((job, profile))
        total += 1
        owners[job["owner_id"]] = owners.get(job["owner_id"], 0) + 1
    return chosen


# How long past its wait limit a job may still go hosted before machines start.
HOSTED_GRACE_SECONDS = 30
# Hosted renders per window at which a machine of ours is cheaper: a 5090 at
# about USD 0.65/h breaks even near 17 turbo clips an hour at USD 0.038 (#918).
HOSTED_WARMS_MACHINE_AT = 5
# Economy shots waiting at which a machine of ours is cheaper than Vivix: each
# clip saves about USD 0.006, so about 40 clips pay for a boot.
ECONOMY_WARMS_MACHINE_AT = 40
# Below this vendor balance hosted stops; jobs stay on our machines.
HOSTED_BALANCE_RESERVE_USD = 1.0
HOSTED_BALANCE_WARN_USD = 10.0


def _first_claim(job, tier, enabled_tiers, now):
    limit = WAIT_LIMITS.get(tier)
    return (
        tier in enabled_tiers
        and limit is not None
        and "hosted_tried" not in job["execution_plan"]
        and now - job["created_at"] < limit + HOSTED_GRACE_SECONDS
    )


def _small_economy(backlog):
    return backlog < ECONOMY_WARMS_MACHINE_AT


@dataclass(frozen=True)
class Scenario:
    """One documentation row; absent facets stay with the named owner.

    Facets intentionally receive only the facts needed at their call site.
    There is no common context, evaluator or permission to perform an action.
    """

    number: int
    owner: str
    offload: Callable[..., bool] | None = None
    hold: Callable[..., bool] | None = None


SCENARIOS = {
    "occasional_click": Scenario(
        1, "scheduling.rebind.offload / hold_for_hosted", _wait_elapsed, _first_claim
    ),
    "sustained_clicks": Scenario(
        2,
        "scheduling.rebind.hold_for_hosted",
        hold=lambda used: not sum(used.values()) >= HOSTED_WARMS_MACHINE_AT,
    ),
    "ready_machine": Scenario(
        3, "control.WorkerControl.claim / modal_admission.dedicated_ready"
    ),
    "small_economy_backlog": Scenario(
        4, "scheduling.rebind.offload", offload=_small_economy
    ),
    "large_economy_backlog": Scenario(
        5, "scheduling.rebind.hold_for_hosted", hold=_small_economy
    ),
    "click_during_economy": Scenario(6, "queue.TaskQueue.schedule_order / claim"),
    "no_machine_can_start": Scenario(7, "dstack_demand_start.start_once"),
    "low_vendor_balance": Scenario(
        8,
        "scheduling.rebind.balance_allows_hosted",
        offload=lambda usd: usd is None or usd >= HOSTED_BALANCE_RESERVE_USD,
    ),
    "hosted_fallback": Scenario(
        9, "worker.WorkerRunner._reroute / _past_deadline / scheduling.rebind.next_plan"
    ),
    "window_full": Scenario(
        10,
        "scheduling.decide.hosted_offload",
        offload=lambda total, owners, job, window: (
            not (total >= window or owners.get(job["owner_id"], 0) >= HOSTED_PER_OWNER)
        ),
    ),
    "backlog_finished": Scenario(
        11,
        "dstack_controller.DstackController._reconcile_observed_node / dstack_capacity.idle_action",
    ),
}


def hosted_has_first_claim(job, tier, enabled_tiers, now):
    """True while the hosted API may still take this job, so no machine starts."""
    return SCENARIOS["occasional_click"].hold(job, tier, enabled_tiers, now)
