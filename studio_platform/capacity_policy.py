"""Shared capacity eligibility contract; no provider, ledger or runtime mutation.

Inputs are the original frozen intent, node and complete retained worker evidence.
A true predicate alone never authorizes a rental, stop, or billing settlement.
"""

import math


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def window_open(intent, node, now):
    deadline = intent.get("hard_deadline")
    if deadline is not None:
        return _finite(deadline) and now < deadline
    from .worker_admission import dstack_policy_reason

    return dstack_policy_reason(intent, node, now) is None


def cold_unadmitted_runtime(binding, workers, instance_id, now, fresh_seconds):
    """No platform execution ever acquired this exact cold preparation.

    Worker rows and the first-ready/incarnation latches are retained history,
    not just current availability. Unknown previously admitted execution must
    never use this exception. The caller still checks ownership and stop intent.
    """
    observed = binding.get("observed_at")
    return (
        not workers
        and bool(instance_id)
        and binding.get("provider_instance_id") == instance_id
        and binding.get("state") in {"starting", "runtime_unconfirmed"}
        and binding.get("runtime_incarnation") is None
        and binding.get("first_runtime_ready_at") is None
        and _finite(observed)
        and 0 <= now - observed <= fresh_seconds
    )
