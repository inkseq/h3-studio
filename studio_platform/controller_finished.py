"""Skip proven-finished stopped nodes in the controller pass (#1252).

Every pass walked every node ever started. Staging had 382, all stopped and
378 destroyed; that loop alone took 15-30 s of each ~50 s pass on a 2-vCPU
host whose hosted turns share the process. A node is remembered only after
it went through the full stopped path once, and skipped only while its node
and intent rows are unchanged. Every remembered node is still revisited in
full every RECHECK_SECONDS, so a missed change is late by at most that much.
"""

from sqlalchemy import select

from .repository import budget_reservations, instance_intents, request_hash

RECHECK_SECONDS = 900


def _memo(controller):
    if not isinstance(getattr(controller, "_finished", None), dict):
        controller._finished = {}
    return controller._finished


def _fingerprint(node, intent):
    return request_hash({"node": dict(node), "intent": dict(intent)})


def _reserved_hold(controller, intent_id):
    """A reserved instance hold may still be released by a later pass."""
    with controller.repo.engine.connect() as connection:
        return (
            connection.execute(
                select(budget_reservations.c.id).where(
                    budget_reservations.c.reference_type == "instance",
                    budget_reservations.c.reference_id == intent_id,
                    budget_reservations.c.state == "reserved",
                )
            ).first()
            is not None
        )


def remember_finished(controller, node, intent, removed, active, unsafe):
    """Called at the end of the stopped path with what that pass found."""
    finished = (
        node["desired_state"] == "stopped"
        and node["runtime_state"] == "stopped"
        and intent.get("state") == "destroyed"
        and (removed or not intent.get("provider_instance_id"))
        and not active
        and not unsafe
        and node["intent_id"] not in controller._cleanup_pending
        and not _reserved_hold(controller, node["intent_id"])
    )
    if finished:
        _memo(controller)[node["intent_id"]] = (
            _fingerprint(node, intent),
            controller.repo.clock(),
        )
    else:
        _memo(controller).pop(node["intent_id"], None)


def finished_skips(controller, nodes):
    """Intent ids to skip this pass; one intent read for all remembered nodes."""
    memo = _memo(controller)
    now = controller.repo.clock()
    for intent_id in [
        key for key, (_, at) in memo.items() if not 0 <= now - at < RECHECK_SECONDS
    ]:
        del memo[intent_id]
    if not memo:
        return set()
    with controller.repo.engine.connect() as connection:
        intents = {
            row["id"]: row
            for row in connection.execute(
                select(instance_intents).where(instance_intents.c.id.in_(list(memo)))
            ).mappings()
        }
    skip = set()
    for node in nodes:
        entry = memo.get(node["intent_id"])
        if entry is None:
            continue
        intent = intents.get(node["intent_id"])
        if intent is not None and entry[0] == _fingerprint(node, intent):
            skip.add(node["intent_id"])
        else:
            del memo[node["intent_id"]]
    return skip
