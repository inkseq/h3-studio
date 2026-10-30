"""Which dstack server owns a run (local or dstack Sky), and provider read helpers.

A binding records the control plane it was applied on. After a migration the
history from the old server is unreadable on the new one, so it can be settled
only by the provider account: absent there means it holds no capacity.
"""

from email.utils import parsedate_to_datetime
import re
import time

# Bindings written before the control plane was recorded ran on the local server.
LEGACY_CONTROL_PLANE = "http://127.0.0.1:3000"


def control_plane(client):
    """The dstack server a client talks to; test doubles count as the local one."""
    endpoint = getattr(client, "endpoint", None)
    return endpoint if isinstance(endpoint, str) else LEGACY_CONTROL_PLANE


def foreign_usage(binding, client, provider_absent, backends):
    """(included, reason) for a node applied on another control plane, else None."""
    if binding.get("control_plane", LEGACY_CONTROL_PLANE) == control_plane(client):
        return None
    backend = binding.get("backend")
    if backend in backends and provider_absent(
        backend, binding.get("provider_instance_id")
    ):
        return False, "dstack_foreign_provider_absent"
    # A live machine left on the old server still blocks starts.
    return True, "dstack_foreign_unconfirmed"


def provider_retry_after(value):
    if not isinstance(value, str) or len(value) > 128:
        return None
    value = value.strip()
    if re.fullmatch(r"[0-9]{1,8}", value):
        return float(value)
    try:
        parsed = parsedate_to_datetime(value)
        if parsed.tzinfo is not None:
            return max(0.0, parsed.timestamp() - time.time())
    except (ValueError, TypeError, OverflowError):
        pass
    return None
