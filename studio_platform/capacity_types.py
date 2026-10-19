"""Value objects retained by the original capacity/history ledger.

This module has no allocator, provider client or cloud lifecycle authority.
"""

from dataclasses import asdict, dataclass
import math
import re

from .repository import money


REMOVAL_CHECK_INTERVAL_SECONDS = 60


def _safe_id(value):
    if not isinstance(value, str) or not re.fullmatch(
        r"[A-Za-z0-9_.:\-]{1,200}", value
    ):
        raise ValueError("invalid_scaler_identifier")
    return value


@dataclass(frozen=True)
class LaunchSpec:
    """Operator manifest references, never credentials, auth URLs or arbitrary JSON."""

    provider: str
    configuration_id: str
    model_id: str
    region: str = ""
    offer_id: str = ""
    image_id: str = ""

    def __post_init__(self):
        for key, value in asdict(self).items():
            if value or key in ("provider", "configuration_id", "model_id"):
                _safe_id(value)
        if self.provider in ("unknown", "mock", "local-cpu"):
            raise ValueError("explicit_cloud_provider_required")


@dataclass(frozen=True)
class ProviderFact:
    """Small verified facts; running describes a VM, never a qualified model slot.

    idle_confirmed must include a fresh dedicated inference-queue inspection.
    absence_confirmed is authoritative operation reconciliation, not an empty
    eventually-consistent search. No raw response or download URL is persisted.
    """

    state: str
    instance_id: str | None = None
    actual_cost_microusd: int | None = None
    idle_confirmed: bool = False
    idle_since: float | None = None
    absence_confirmed: bool = False
    provider_status: str | None = None
    preparation_stage: str | None = None

    def __post_init__(self):
        if self.state not in {
            "unknown",
            "not_created",
            "starting",
            "running",
            "destroyed",
        }:
            raise ValueError("invalid_provider_fact")
        if self.instance_id is not None:
            _safe_id(self.instance_id)
        if self.actual_cost_microusd is not None:
            money(self.actual_cost_microusd)
        if (
            type(self.idle_confirmed) is not bool
            or type(self.absence_confirmed) is not bool
        ):
            raise ValueError("invalid_provider_proof")
        if self.idle_since is not None and (
            not isinstance(self.idle_since, (int, float))
            or not math.isfinite(self.idle_since)
        ):
            raise ValueError("invalid_provider_idle_time")
        if (
            self.state in ("starting", "running", "destroyed")
            and self.instance_id is None
        ):
            raise ValueError("provider_instance_id_required")
        if self.state == "not_created" and (
            not self.absence_confirmed
            or self.instance_id is not None
            or self.actual_cost_microusd != 0
        ):
            raise ValueError("authoritative_not_created_proof_required")
        if self.provider_status not in (
            None,
            "PENDING",
            "FAILED",
            "STOPPED",
            "RUNNING",
        ) or self.preparation_stage not in (
            None,
            "provider_preparing",
            "configuring_ssh",
        ):
            raise ValueError("invalid_provider_preparation_fact")
