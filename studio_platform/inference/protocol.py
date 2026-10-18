"""Worker-facing engine contract; no queue, runtime or credential initialization.

The business queue owns execution identity and submission intent. Adapters must
not turn an unknown execution into a new submission or a cancellation
acknowledgement into proof of a terminal result. See GENERATION-CONTRACT.md.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any, Callable, Mapping, Protocol


TAG = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
TASK = re.compile(r"^[A-Za-z0-9_-]{1,200}$")

# Diagnostic labels only, never execution/retry/stop authority. The pinned
# WanGP API discards exception classes, so categories describe known message
# signatures rather than claiming a recovered exception type or root cause.
INFERENCE_FAILURE_CODES = frozenset(
    f"wangp_{stage}_{category}"
    for stage in ("validation", "generation", "runtime", "unknown")
    for category in (
        "unclassified",
        "cuda_out_of_memory",
        "tensor_shape_mismatch",
        "media_decode_failed",
        "dependency_missing",
        "seed_out_of_range",
    )
)


def safe_failure_code(value):
    """Accept only the closed vocabulary; never persist arbitrary adapter text."""
    return value if type(value) is str and value in INFERENCE_FAILURE_CODES else None


class BackendError(Exception):
    """Stable message only. Do not propagate URL, response body, prompt or tokens."""


class NotReady(BackendError):
    pass


class RenderCacheCapacityExceeded(BackendError):
    """Local CPU cache cannot admit another attempt; operator action is required."""


class SubmissionUncertain(BackendError):
    pass


class SubmissionRejected(BackendError):
    pass


@dataclass(frozen=True)
class Outcome:
    state: str
    task_id: str | None = None
    actual_cost_microusd: int | None = None
    error_code: str | None = None


class InferenceBackend(Protocol):
    # fmt: off
    """Structural contract for enabled adapters; not a backend registry.

The worker commits intent before submit, validates fetched media, and owns
durable publication. Optional cost_resolver and close hooks remain optional.
"""
    # fmt: on

    enabled: bool
    kind: str
    slot_key: str

    def prepare(
        self,
        job: Mapping[str, Any],
        tag: str,
        store: Any,
        heartbeat: Callable[[], None],
    ) -> Any:
        """Validate/stage the immutable request without starting inference."""
        ...

    def submit(self, prepared: Any, tag: str) -> str:
        """Submit once after intent; distinguish rejection from uncertainty."""
        ...

    def reconcile(self, tag: str, task_id: str | None = None) -> Outcome:
        """Resolve the original attempt without submitting another one."""
        ...

    def poll(self, tag: str, task_id: str) -> Outcome: ...

    def cancel(self, tag: str, task_id: str) -> bool:
        """Acknowledge an attempt-specific request, not necessarily a stop."""
        ...

    def fetch(
        self,
        job: Mapping[str, Any],
        tag: str,
        task_id: str,
        target_dir: Path,
        heartbeat: Callable[[], None],
    ) -> Mapping[str, Path]:
        """Retrieve the original result; never generate to repair collection."""
        ...


class IdleProbe(Protocol):
    # fmt: off
    """Dedicated-engine readiness probe, separate from inference quality.

Only exactly True confirms current idle evidence. False or an exception cannot
authorize a claim. An empty queue never resolves an unknown prior submission.
"""
    # fmt: on

    def is_idle(self) -> bool: ...
