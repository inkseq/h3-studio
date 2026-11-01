"""Per-lane start health (#931 step 2): a failing lane cools down alone.

A machine the provider allocated that stopped before its runtime was ever
ready is a failed start of its backend. A start the provider never allocated
keeps the demand start's own no-allocation backoff, and a machine that became
ready and idled out unserved is a demand question; neither counts here. Each backend backs off on
its own, doubling from 5 to 30 minutes and forgetting its failures after 3
hours without one. These are Kubernetes
cluster-autoscaler's node-group backoff defaults (ideas only, no code copied).
Derived from the original node, attempt and worker rows; no new table, and
nothing here starts, stops or bills a machine.
"""

INITIAL_SECONDS = 300
MAX_SECONDS = 1800
RESET_SECONDS = 10800


def lane(node, now):
    """The backend to blame for this start, or None if it is not a lane fault."""
    from ..capacity_health import finite

    binding = node["payload"].get("dstack", {})
    allocated = node.get("provider_instance_id") or binding.get("provider_instance_id")
    backend = binding.get("backend")
    if (
        isinstance(backend, str)
        and isinstance(allocated, str)
        and allocated
        and not finite(binding.get("first_runtime_ready_at"), now)
    ):
        return backend
    return None


def failed_starts(nodes, attempt_rows, workers, now):
    """(stopped, intent_id, backend) of allocated starts that never got ready."""
    from ..capacity_health import node_attempts, unserved_stop

    found = []
    for node in nodes:
        backend = lane(node, now)
        if backend is None:
            continue
        _, related = node_attempts(node, attempt_rows, workers)
        cycle = unserved_stop(node, related, now)
        if cycle is not None:
            found.append((cycle[1], node["intent_id"], backend))
    return sorted(found)


def lane_backoff(failures, now):
    """Backend -> cooling-until time, for backends whose recent starts failed."""
    runs = {}
    for stopped, _, backend in sorted(failures):
        count, last = runs.get(backend, (0, None))
        if last is not None and stopped - last > RESET_SECONDS:
            count = 0
        runs[backend] = (count + 1, stopped)
    cooling = {}
    for backend, (count, last) in runs.items():
        until = last + min(INITIAL_SECONDS * 2 ** (count - 1), MAX_SECONDS)
        if now - last <= RESET_SECONDS and until > now:
            cooling[backend] = until
    return cooling


def cooling_backends(connection, tenant, now):
    """Backends to skip for a new start now; read-only."""
    from ..capacity_health import capacity_rows

    nodes, attempt_rows, workers = capacity_rows(connection, tenant)
    return lane_backoff(failed_starts(nodes, attempt_rows, workers, now), now)
