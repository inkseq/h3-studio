"""Original native capacity request and validation identities; no provider I/O."""

from __future__ import annotations

from dataclasses import dataclass
import copy
import hashlib
import json
import math
import re
from uuid import UUID


from .inference.wangp_contract import safe_model_progress
from .worker_boot import boot_commands, boot_env, boot_record, boot_valid
from .providers.registry import VASTAI

from .dstack_inventory import (
    SUPPORTED,
)

DSTACK_VERSION = "0.22.5"
BACKENDS = SUPPORTED
OWNER = "sixnine"
SHA256 = re.compile(r"[0-9a-f]{64}")
IDENTIFIER = re.compile(r"[A-Za-z0-9_.-]{1,200}")
TERMINAL = frozenset({"done", "failed", "aborted", "terminated"})
RUN_STATUSES = TERMINAL | frozenset(
    {"pending", "submitted", "provisioning", "running", "terminating", "stopping"}
)
VAST_INSTANCES_URL = "https://console.vast.ai/api/v1/instances/"
PROVIDER_READ_ATTEMPTS = 3
PROVIDER_READ_MAX_DELAY = 5.0
NATIVE_REASON_CODES = frozenset(
    {
        "wangp_model_load_failed",
        "wangp_model_load_unobserved",
        "dstack_runtime_preparing",
        "dstack_runtime_downloading_weights",
        "dstack_runtime_starting_runtime",
        "dstack_runtime_loading_model",
        "dstack_runtime_bootstrap_failed",
        "dstack_runtime_bootstrap_reconciliation_required",
        "dstack_runtime_identity_invalid",
        "dstack_runtime_path_invalid",
        "dstack_runtime_link_forbidden",
        "dstack_runtime_binding_invalid",
        "dstack_runtime_binding_changed",
        "dstack_runtime_run_not_running",
        "dstack_runtime_allocation_mismatch",
        "dstack_runtime_configuration_mismatch",
        "dstack_runtime_configuration_missing",
        "dstack_runtime_configuration_unavailable",
        "dstack_runtime_config_invalid",
        "dstack_runtime_port_range_invalid",
        "dstack_runtime_retained_port_invalid",
        "dstack_runtime_port_conflict",
        "dstack_runtime_port_changed",
        "dstack_runtime_stopped_port_unallocated",
        "dstack_runtime_ports_exhausted",
        "dstack_runtime_known_hosts_invalid",
        "dstack_runtime_image_unpinned",
        "dstack_runtime_source_changed",
        "dstack_runtime_source_index_invalid",
        "dstack_runtime_source_entry_invalid",
        "dstack_runtime_source_profile_invalid",
        "dstack_runtime_source_binding_invalid",
        "dstack_runtime_source_set_invalid",
        "dstack_runtime_source_selection_missing",
        "dstack_runtime_prepared_source_required",
        "dstack_runtime_ssh_coordinates_invalid",
        "dstack_runtime_ssh_coordinates_unavailable",
        "dstack_runtime_observation_busy",
        "dstack_runtime_receipt_changed",
        "dstack_runtime_receipt_phase_invalid",
        "dstack_runtime_existing_receipt_requires_reconciliation",
        "dstack_runtime_host_limit_reached",
        "dstack_runtime_ssh_unavailable_or_host_key_untrusted",
        "dstack_runtime_host_identity_changed",
        "dstack_runtime_recovery_material_missing",
        "dstack_runtime_upload_reconciliation_required",
        "dstack_runtime_upload_unconfirmed",
        "dstack_runtime_launch_requires_reconciliation",
        "dstack_runtime_bootstrap_response_unknown",
        "dstack_runtime_remote_identity_unconfirmed",
        "dstack_runtime_physical_gpu_invalid",
        "dstack_runtime_physical_gpu_changed",
        "dstack_runtime_private_endpoint_mismatch",
        "dstack_runtime_incarnation_changed",
        "dstack_native_runtime_unconfirmed",
        "dstack_runtime_report_invalid",
        "dstack_runtime_tunnel_port_in_use",
        "dstack_runtime_tunnel_failed",
        "dstack_runtime_readiness_unreachable",
        "dstack_runtime_client_config_changed",
        "dstack_runtime_slot_register_failed",
    }
)


def native_preparation(value):
    """Revalidate the progress seam even when an injected callback is untrusted."""
    if not isinstance(value, dict):
        return None
    from .wangp_bootstrap import safe_preparation_report
    from .native_bootstrap import BOOT_FAILURE_CODES

    source = {
        "phase": value.get("phase"),
        "started_unix": value.get("started_at"),
        "phase_started_unix": value.get("phase_started_at"),
        "updated_unix": value.get("updated_at"),
    }
    download = value.get("download")
    if isinstance(download, dict):
        source.update(
            {
                key: download.get(key)
                for key in (
                    "bytes_complete",
                    "bytes_received",
                    "bytes_total",
                    "files_complete",
                    "files_total",
                )
            }
        )
    result = safe_preparation_report(source)
    result["model"] = safe_model_progress(value.get("model"))
    if (
        type(value.get("reason_code")) is str
        and value["reason_code"] in BOOT_FAILURE_CODES
    ):
        result["reason_code"] = value["reason_code"]
        result["failure_phase"] = safe_preparation_report(
            {"phase": value.get("failure_phase")}
        )["phase"]
    return result


def image_preparation(submission):
    """Pinned dstack 0.22.5 ImagePullProgress; no Docker text or image URLs."""
    raw = (
        submission.get("image_pull_progress") if isinstance(submission, dict) else None
    )
    keys = ("downloaded_bytes", "extracted_bytes", "total_bytes")
    if (
        not isinstance(raw, dict)
        or type(raw.get("is_total_bytes_final")) is not bool
        or any(
            type(raw.get(key)) is not int or not 0 <= raw[key] <= 1024**4
            for key in keys
        )
    ):
        return None
    return {key: raw[key] for key in (*keys, "is_total_bytes_final")}


class DstackError(ValueError):
    """Static diagnostics only; do not wrap provider response/exception text."""


class DstackReadRateLimited(DstackError):
    """A rejected read is unconfirmed inventory, never evidence of no stock."""

    def __init__(self, retry_after=None):
        super().__init__("dstack_provider_rate_limited")
        self.retry_after = min(86400, max(1, math.ceil(retry_after or 1)))


def _require(condition, code):
    if not condition:
        raise DstackError(code)


def _hash(value):
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


def _uuid(value):
    _require(isinstance(value, str), "dstack_identity_invalid")
    try:
        _require(str(UUID(value)) == value, "dstack_identity_invalid")
    except (ValueError, TypeError, AttributeError):
        raise DstackError("dstack_identity_invalid") from None
    return value


@dataclass(frozen=True)
class CapacityRequest:
    intent_id: str
    backend: str
    profile_id: str
    mode: str
    model_id: str
    configuration_id: str
    manifest_digest: str
    image: str
    gpu_names: tuple[str, ...]
    memory_gib: int
    disk_gib: int
    max_price_microusd: int
    created_at: float
    hard_deadline: float | None
    max_duration_s: int | None
    cpu_count: int = 4
    gpu_memory_gib: int = 30
    gpu_count: int = 1
    min_reliability: float = 0.90
    regions: tuple[str, ...] = ()
    lifecycle_version: int = 1
    host_selection: dict | None = None
    test_max_duration_s: int | None = None
    test_task_id: str | None = None
    boot_token: str | None = None
    boot_url: str | None = None

    def __post_init__(self):
        _uuid(self.intent_id)
        _require(boot_valid(self.boot_token, self.boot_url), "dstack_boot_invalid")
        _require(self.backend in BACKENDS, "dstack_backend_unsupported")
        _require(self.mode in {"fl", "ref"}, "dstack_mode_invalid")
        for identity in (self.profile_id, self.model_id, self.configuration_id):
            _require(
                isinstance(identity, str) and IDENTIFIER.fullmatch(identity),
                "dstack_identity_invalid",
            )
        _require(
            isinstance(self.manifest_digest, str)
            and SHA256.fullmatch(self.manifest_digest),
            "dstack_manifest_invalid",
        )
        _require(
            isinstance(self.image, str)
            and re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9./:_-]*@sha256:[0-9a-f]{64}", self.image
            ),
            "dstack_image_digest_required",
        )
        _require(
            type(self.gpu_count) is int and self.gpu_count == 1,
            "dstack_single_gpu_required",
        )
        for count in (
            self.memory_gib,
            self.disk_gib,
            self.max_price_microusd,
            self.cpu_count,
            self.gpu_memory_gib,
        ):
            _require(type(count) is int and count > 0, "dstack_resource_limit_invalid")
        _require(
            type(self.lifecycle_version) is int and self.lifecycle_version in {1, 2},
            "dstack_lifecycle_invalid",
        )
        _require(
            self.test_max_duration_s is None
            or self.lifecycle_version == 2
            and type(self.test_max_duration_s) is int
            and 60 <= self.test_max_duration_s <= 86400,
            "dstack_test_duration_invalid",
        )
        from .dstack_test_budget import valid_task_id

        _require(
            self.test_task_id is None
            or self.test_max_duration_s is not None
            and valid_task_id(self.test_task_id),
            "dstack_test_task_invalid",
        )
        _require(
            type(self.created_at) in (int, float)
            and math.isfinite(self.created_at)
            and self.created_at > 0,
            "dstack_deadline_invalid",
        )
        if self.lifecycle_version == 2:
            # Only explicitly new continuing intents have no fixed deadline.
            # Old requests remain finite and retain their frozen spec identity.
            _require(
                self.hard_deadline is None and self.max_duration_s is None,
                "dstack_lifecycle_invalid",
            )
        else:
            _require(
                type(self.max_duration_s) is int and 0 < self.max_duration_s <= 86400,
                "dstack_duration_invalid",
            )
            _require(
                type(self.hard_deadline) in (int, float)
                and math.isfinite(self.hard_deadline)
                and self.created_at < self.hard_deadline,
                "dstack_deadline_invalid",
            )
        _require(
            isinstance(self.gpu_names, tuple)
            and 1 <= len(self.gpu_names) <= 8
            and all(
                isinstance(n, str) and re.fullmatch(r"[A-Za-z0-9 _.-]{1,100}", n)
                for n in self.gpu_names
            ),
            "dstack_gpu_selection_invalid",
        )
        _require(
            isinstance(self.regions, tuple)
            and len(self.regions) <= 20
            and all(
                isinstance(n, str) and IDENTIFIER.fullmatch(n) for n in self.regions
            ),
            "dstack_region_invalid",
        )
        _require(
            type(self.min_reliability) in (int, float)
            and math.isfinite(self.min_reliability)
            and 0.9 <= self.min_reliability <= 1,
            "dstack_reliability_invalid",
        )
        if self.host_selection is not None:
            _require(self.backend == VASTAI.id, "dstack_host_backend_invalid")
            try:
                from .host_policy import selection

                selection(self.host_selection)
            except ValueError:
                raise DstackError("dstack_host_selection_invalid") from None

    @property
    def run_name(self):
        return "sixnine-" + self.intent_id.replace("-", "")

    def run_spec(self, ssh_key_pub):
        _require(
            isinstance(ssh_key_pub, str)
            and re.fullmatch(
                r"ssh-(?:ed25519|rsa) [A-Za-z0-9+/=]+(?: [^\r\n]*)?", ssh_key_pub
            ),
            "dstack_public_ssh_key_invalid",
        )
        # The image contains prepared dependencies. CPU uploads the existing
        # hash-bound bundle and starts the loopback launcher through SSH.
        configuration = {
            "type": "task",
            "name": self.run_name,
            "image": self.image,
            "commands": boot_commands(self.boot_token),
            "backends": [self.backend],
            "resources": {
                "cpu": {"arch": "x86", "count": f"{self.cpu_count}.."},
                "memory": f"{self.memory_gib}GB..",
                "disk": {"size": f"{self.disk_gib}GB.."},
                "gpu": {
                    "vendor": "nvidia",
                    "name": list(self.gpu_names),
                    "count": 1,
                    "memory": f"{self.gpu_memory_gib}GB..",
                },
            },
            # Pinned 0.22.5 null means inherit the profile; "off" explicitly
            # disables only the new intent's fixed running-duration limit.
            "max_price": self.max_price_microusd / 1_000_000,
            "max_duration": (self.test_max_duration_s or "off")
            if self.lifecycle_version == 2
            else self.max_duration_s,
            "retry": False,
            "spot_policy": "on-demand",
            "dstack": False,
            "env": boot_env(self.intent_id, self.boot_token, self.boot_url),
            "ports": [],
            "stop_duration": 300,
            "regions": list(self.regions) if self.regions else None,
            "tags": {
                "sixnine_owner": OWNER,
                "sixnine_intent": self.intent_id,
                "sixnine_profile": self.profile_id,
                "sixnine_mode": self.mode,
                "sixnine_manifest": self.manifest_digest,
            },
        }
        if self.backend == VASTAI.id:
            configuration["backend_options"] = [
                {
                    "type": "vastai",
                    "offer_order": "price",
                    "min_reliability": self.min_reliability,
                }
            ]
        if self.test_task_id is not None:
            configuration["tags"]["inkseq_test_task"] = self.test_task_id
        spec = {
            "run_name": self.run_name,
            "configuration": configuration,
            "ssh_key_pub": ssh_key_pub,
        }
        if self.host_selection is not None:
            # Native 0.22.5 uses this selector for backend offers AND fleet
            # reuse. Editing plan.offers would not constrain its later apply.
            spec["profile"] = {
                "instance_types": [
                    row["offer_id"] for row in self.host_selection["offers"]
                ]
            }
        configuration["tags"]["sixnine_spec"] = _hash(spec)
        return spec

    def binding(self, ssh_key_pub):
        spec = self.run_spec(ssh_key_pub)
        value = {
            "intent_id": self.intent_id,
            "run_name": self.run_name,
            "backend": self.backend,
            "profile_id": self.profile_id,
            "mode": self.mode,
            "model_id": self.model_id,
            "configuration_id": self.configuration_id,
            "manifest_digest": self.manifest_digest,
            "created_at": self.created_at,
            "hard_deadline": self.hard_deadline,
            "spec_digest": _hash(spec),
            "run_spec": spec,
        }
        if self.lifecycle_version == 2:
            value["lifecycle_version"] = 2
        if self.host_selection is not None:
            value["host_selection"] = copy.deepcopy(self.host_selection)
        value.update(boot_record(self.boot_token, self.created_at))
        return value


def request_from_document(document):
    """Decode a retained request without mutating its immutable JSON receipt."""
    document = copy.deepcopy(document)
    for key in ("gpu_names", "regions"):
        if key in document:
            document[key] = tuple(document[key])
    return CapacityRequest(**document)


# Preserve the original public import and pickle identity of moved definitions.
native_preparation.__module__ = "studio_platform.dstack_capacity"
image_preparation.__module__ = "studio_platform.dstack_capacity"
DstackError.__module__ = "studio_platform.dstack_capacity"
DstackReadRateLimited.__module__ = "studio_platform.dstack_capacity"
_require.__module__ = "studio_platform.dstack_capacity"
_hash.__module__ = "studio_platform.dstack_capacity"
_uuid.__module__ = "studio_platform.dstack_capacity"
CapacityRequest.__module__ = "studio_platform.dstack_capacity"
request_from_document.__module__ = "studio_platform.dstack_capacity"
