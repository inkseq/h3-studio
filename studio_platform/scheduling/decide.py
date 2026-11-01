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

Later steps add hosted offload and the economy class here.
"""

from dataclasses import dataclass


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
