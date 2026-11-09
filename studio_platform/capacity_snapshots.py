"""Sanitized provider responses the controller acted on, for incident replay.

The controller reads dstack runs and provider inventories every tick and keeps
only its own conclusions. To replay an incident in the simulator we also need
what the provider said: run status transitions, the provisioning identity, the
instance's presence in the account inventory and error codes.

``RecordingClient`` wraps the existing client without changing any call: it
returns the same objects and re-raises the same exceptions, and only copies an
allowlisted subset into a bounded in-memory buffer. The timeline recorder
persists that subset per machine, on change only (capacity_timeline.py).

Never kept: credentials, tokens, IPs and hostnames, SSH keys and users, emails,
project or account names, run specs, ``env`` and free-text messages. Values
are allowlisted by field and checked again by pattern.
"""

from __future__ import annotations

from collections import deque
import math
import re
import threading

# Plain identifiers, statuses, regions, instance types and timestamps.
_SAFE_TEXT = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._:/+-]{0,99}")
_IPV4 = re.compile(r"(?<![0-9])(?:[0-9]{1,3}\.){3}[0-9]{1,3}(?![0-9])")
_IPV6 = re.compile(r"[0-9A-Fa-f]{0,4}(?::[0-9A-Fa-f]{0,4}){2,7}")
# A long unbroken run mixing letters and digits is treated as a secret or token
# (reason codes such as dstack_download_too_slow are words joined by "_").
_TOKEN = re.compile(r"[A-Za-z0-9+/=]{24,}")
_CODE = re.compile(r"[a-z][a-z0-9_]{0,79}")
_KEY_HINTS = ("token", "secret", "password", "key", "auth", "cred", "ssh", "env")

RUN_FIELDS = (
    "id",
    "run_name",
    "status",
    "termination_reason",
    "submitted_at",
    "last_processed_at",
    "cost",
    "deleted",
)
SUBMISSION_FIELDS = (
    "status",
    "termination_reason",
    "exit_status",
    "submitted_at",
    "finished_at",
    "submission_num",
    "deployment_num",
)
PROVISIONING_FIELDS = ("backend", "instance_id", "region", "availability_zone", "price")
_WRAPPED = frozenset({"get", "provider_inventory", "apply", "stop"})
BUFFER_LIMIT = 2000
BUFFER_SECONDS = 3600


def safe_text(value):
    """The value if it is a short plain identifier, else None."""
    if not isinstance(value, str) or not _SAFE_TEXT.fullmatch(value):
        return None
    if _IPV4.search(value) or _IPV6.fullmatch(value):
        return None
    for run in _TOKEN.findall(value):
        if any(c.isdigit() for c in run) and any(c.isalpha() for c in run):
            return None
    return value


def _scalar(value):
    if value is None or type(value) is bool:
        return value
    if type(value) is int:
        return value
    if type(value) is float:
        return value if math.isfinite(value) else None
    return safe_text(value)


def _pick(source, fields):
    result = {}
    if not isinstance(source, dict):
        return result
    for name in fields:
        if name in source and not any(hint in name for hint in _KEY_HINTS):
            value = _scalar(source[name])
            if value is not None or source[name] is None:
                result[name] = value
    return result


def sanitize_run(run):
    """The allowlisted identity and status fields of one dstack run document."""
    if not isinstance(run, dict):
        return {"present": False}
    value = _pick(run, RUN_FIELDS)
    submission = run.get("latest_job_submission")
    if isinstance(submission, dict):
        picked = _pick(submission, SUBMISSION_FIELDS)
        data = submission.get("job_provisioning_data")
        if isinstance(data, dict):
            provisioning = _pick(data, PROVISIONING_FIELDS)
            kind = data.get("instance_type")
            if isinstance(kind, dict):
                instance = _pick(kind, ("name",))
                resources = kind.get("resources")
                gpus = resources.get("gpus") if isinstance(resources, dict) else None
                if isinstance(gpus, list):
                    names = [
                        safe_text(gpu.get("name"))
                        for gpu in gpus[:8]
                        if isinstance(gpu, dict)
                    ]
                    instance["gpus"] = [name for name in names if name]
                    instance["gpu_count"] = len(gpus)
                if isinstance(resources, dict) and type(resources.get("spot")) is bool:
                    instance["spot"] = resources["spot"]
                provisioning["instance_type"] = instance
            picked["job_provisioning_data"] = provisioning
        value["latest_job_submission"] = picked
    return value


def error_code(error):
    """A static diagnostic code (DstackError) or the exception type, never text."""
    code = getattr(error, "code", None) or (error.args[0] if error.args else None)
    if isinstance(code, str) and _CODE.fullmatch(code):
        return code
    return type(error).__name__[:80]


def inventory_presence(inventory, instance_id, now):
    """Whether one instance is in a backend's account inventory read."""
    if inventory is None:
        return None
    if inventory.get("error"):
        return {"backend": inventory["backend"], "error": inventory["error"]}
    observed = inventory.get("observed_at")
    return {
        "backend": inventory["backend"],
        "complete": inventory.get("complete") is True,
        "present": instance_id in inventory["ids"],
        "observed_age_s": (
            round(now - observed, 1)
            if type(observed) in (int, float) and math.isfinite(observed)
            else None
        ),
    }


class SnapshotBuffer:
    """Recent sanitized responses, bounded in count and age; thread-safe."""

    def __init__(self, clock):
        self.clock = clock
        self._lock = threading.Lock()
        self._runs = deque(maxlen=BUFFER_LIMIT)
        self._inventories = {}
        self._seq = 0

    def _add(self, entry):
        with self._lock:
            self._seq += 1
            self._runs.append((self._seq, self.clock(), *entry))

    def note_run(self, identity, run, error):
        try:
            name = identity.get("run_name")
            run_id = identity.get("run_id")
            if isinstance(run, dict):
                name = run.get("run_name") or name
                run_id = run.get("id") or run_id
            data = {"error": error_code(error)} if error else sanitize_run(run)
            self._add(("run", name, run_id, data))
        except Exception:
            pass  # Recording never changes the provider call.

    def note_call(self, kind, name, error):
        try:
            data = {"ok": True} if error is None else {"error": error_code(error)}
            self._add((kind, name, None, data))
        except Exception:
            pass

    def note_inventory(self, backend, inventory, error):
        try:
            if not isinstance(backend, str):
                return
            if error is not None:
                value = {"backend": backend, "error": error_code(error)}
            else:
                ids = (
                    inventory.get("instance_ids")
                    if isinstance(inventory, dict)
                    else None
                )
                value = {
                    "backend": backend,
                    "complete": inventory.get("complete") is True,
                    "observed_at": inventory.get("observed_at"),
                    "ids": frozenset(str(item) for item in ids or ()),
                }
            value["at"] = self.clock()
            with self._lock:
                self._inventories[backend] = value
        except Exception:
            pass

    def inventory(self, backend):
        with self._lock:
            return self._inventories.get(backend)

    def take(self, run_name, run_id):
        """Entries for one run, oldest first; removes them and anything stale."""
        with self._lock:
            now = self.clock()
            kept, taken = deque(maxlen=BUFFER_LIMIT), []
            for entry in self._runs:
                _, at, _, name, identity, _ = entry
                if (name and name == run_name) or (identity and identity == run_id):
                    taken.append(entry)
                elif now - at <= BUFFER_SECONDS:
                    kept.append(entry)
            self._runs = kept
        return [(at, kind, data) for _, at, kind, _, _, data in taken]


class RecordingClient:
    """The original provider client; reads are copied into a SnapshotBuffer."""

    _capacity_recording = True

    def __init__(self, client, buffer):
        object.__setattr__(self, "_client", client)
        object.__setattr__(self, "_buffer", buffer)

    def __getattr__(self, name):
        return getattr(self._client, name)

    def __setattr__(self, name, value):
        # The wrapped calls stay on the wrapper (a test may patch them);
        # everything else is the original client's state.
        if name.startswith("_") or name in _WRAPPED:
            object.__setattr__(self, name, value)
        else:
            setattr(self._client, name, value)

    def __delattr__(self, name):
        if name in self.__dict__:
            object.__delattr__(self, name)
        else:
            delattr(self._client, name)

    def get(self, *args, **kwargs):
        try:
            run = self._client.get(*args, **kwargs)
        except Exception as error:
            self._buffer.note_run(kwargs, None, error)
            raise
        self._buffer.note_run(kwargs, run, None)
        return run

    def provider_inventory(self, backend, *args, **kwargs):
        try:
            value = self._client.provider_inventory(backend, *args, **kwargs)
        except Exception as error:
            self._buffer.note_inventory(backend, None, error)
            raise
        self._buffer.note_inventory(backend, value, None)
        return value

    def apply(self, spec, *args, **kwargs):
        name = spec.get("run_name") if isinstance(spec, dict) else None
        try:
            value = self._client.apply(spec, *args, **kwargs)
        except Exception as error:
            self._buffer.note_call("create", name, error)
            raise
        self._buffer.note_call("create", name, None)
        return value

    def stop(self, name, *args, **kwargs):
        try:
            value = self._client.stop(name, *args, **kwargs)
        except Exception as error:
            self._buffer.note_call("stop", name, error)
            raise
        self._buffer.note_call("stop", name, None)
        return value


def install(service, clock):
    """Wrap the capacity client and the service inventory reader once.

    Returns the shared buffer. A second controller on the same capacity (a
    restart in tests) reuses the existing wrapper instead of stacking another.
    """
    capacity = service.capacity
    client = capacity.client
    if getattr(client, "_capacity_recording", False):
        buffer = client._buffer
    else:
        buffer = SnapshotBuffer(clock)
        capacity.client = RecordingClient(client, buffer)
    reader = getattr(service, "provider_inventory", None)
    if callable(reader) and not getattr(reader, "_capacity_recording", False):

        def provider_inventory(backend, *args, **kwargs):
            try:
                value = reader(backend, *args, **kwargs)
            except Exception as error:
                buffer.note_inventory(backend, None, error)
                raise
            buffer.note_inventory(backend, value, None)
            return value

        provider_inventory._capacity_recording = True
        service.provider_inventory = provider_inventory
    return buffer
