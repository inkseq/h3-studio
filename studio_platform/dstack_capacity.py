"""Pinned dstack capacity transport; business intents and obligations stay external.

Importing this module never connects to dstack or a provider. A caller injects
the existing transaction-backed ledger, not a second rental database. Each
initial run has one physical GPU and a persistent private WanGP Session.
"""

from __future__ import annotations

from dataclasses import dataclass
import argparse
import copy
from email.utils import parsedate_to_datetime
import hashlib
import importlib
import json
import math
from pathlib import Path, PurePosixPath
import re
import random
import time
from typing import Protocol
from urllib.parse import urlsplit
from uuid import UUID

import httpx

from .inference.wangp_contract import HostReadiness, safe_model_progress
from .capacity_memory import vast_ram_floor

DSTACK_VERSION = "0.22.5"
BACKENDS = frozenset({"vastai", "runpod"})
OWNER = "sixnine"
SHA256 = re.compile(r"[0-9a-f]{64}")
IDENTIFIER = re.compile(r"[A-Za-z0-9_.-]{1,200}")
TERMINAL = frozenset({"done", "failed", "aborted", "terminated"})
RUN_STATUSES = TERMINAL | frozenset(
    {"pending", "submitted", "provisioning", "running", "terminating", "stopping"}
)
VAST_INSTANCES_URL = "https://console.vast.ai/api/v1/instances/"
RUNPOD_GRAPHQL_URL = "https://api.runpod.io/graphql"
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


def _provider_retry_after(value):
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

    def __post_init__(self):
        _uuid(self.intent_id)
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
            _require(self.backend == "vastai", "dstack_host_backend_invalid")
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
            "commands": ["exec sleep infinity"],
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
            "env": {},
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
        if self.backend == "vastai":
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
        return value


def request_from_document(document):
    """Decode a retained request without mutating its immutable JSON receipt."""
    document = copy.deepcopy(document)
    for key in ("gpu_names", "regions"):
        if key in document:
            document[key] = tuple(document[key])
    return CapacityRequest(**document)


class DstackStore(Protocol):
    """Implement against original capacity intents under its transactional lock.

    begin_apply commits intent and reservation before HTTP and succeeds once.
    begin_stop commits once, checking original jobs/attempts/collection holds,
    worker retirement and matching immutable run identity in that transaction.
    record merges observations; it MUST retain run_id/incarnation and journals.
    Stop retries require a fresh exact-owned RUNNING observation and a bounded
    durable retry journal under the same original obligation lock. A false gate
    permits no network side effect. No missing row adoption.
    """

    def load(self, intent_id: str) -> dict: ...
    def begin_apply(self, intent_id: str, binding: dict) -> bool: ...
    def record(self, intent_id: str, observation: dict) -> None: ...
    def begin_stop(
        self, intent_id: str, run_id: str, *, retry: bool = False
    ) -> bool: ...


class DstackClient:
    """Exact 0.22.5 REST requests, no automatic side-effect retry."""

    def __init__(
        self,
        endpoint,
        token,
        project,
        *,
        session=None,
        timeout=30,
        provider_session=None,
    ):
        parsed = urlsplit(endpoint)
        _require(
            parsed.scheme == "https"
            or parsed.scheme == "http"
            and parsed.hostname in {"127.0.0.1", "localhost", "::1"},
            "dstack_endpoint_invalid",
        )
        _require(
            not parsed.username
            and not parsed.password
            and not parsed.query
            and not parsed.fragment
            and parsed.path in {"", "/"},
            "dstack_endpoint_invalid",
        )
        _require(
            isinstance(project, str) and re.fullmatch(r"[a-z][a-z0-9-]{0,62}", project),
            "dstack_project_invalid",
        )
        _require(
            isinstance(token, str)
            and 16 <= len(token) <= 4096
            and not any(c.isspace() for c in token),
            "dstack_token_invalid",
        )
        _require(
            type(timeout) in (int, float) and 0 < timeout <= 120,
            "dstack_timeout_invalid",
        )
        self.endpoint, self.project, self.timeout = (
            endpoint.rstrip("/"),
            project,
            timeout,
        )
        self._headers = {"Authorization": "Bearer " + token}
        self._session = session or httpx.Client(follow_redirects=False, trust_env=False)
        self._owns_session = session is None
        self._provider_session, self._owns_provider_session = (
            provider_session,
            provider_session is None,
        )

    def close(self):
        if self._owns_session:
            self._session.close()
        if self._owns_provider_session and self._provider_session is not None:
            self._provider_session.close()

    def _post(self, path, body, *, absent=False, timeout=None):
        try:
            if timeout is None:
                response = self._session.post(
                    self.endpoint + path,
                    json=body,
                    headers=self._headers,
                    timeout=self.timeout,
                )
            else:
                deadline = time.monotonic() + min(timeout, self.timeout)
                with self._session.stream(
                    "POST",
                    self.endpoint + path,
                    json=body,
                    headers=self._headers,
                    timeout=min(timeout, self.timeout),
                    follow_redirects=False,
                ) as incoming:
                    content = bytearray()
                    for chunk in incoming.iter_bytes(chunk_size=65536):
                        _require(
                            time.monotonic() < deadline
                            and len(content) + len(chunk) <= 8 * 1024**2,
                            "dstack_response_limit",
                        )
                        content.extend(chunk)
                    _require(time.monotonic() < deadline, "dstack_api_unavailable")
                    response = httpx.Response(
                        incoming.status_code, content=bytes(content)
                    )
            if absent and response.status_code == 404:
                return None
            _require(len(response.content) <= 8 * 1024**2, "dstack_response_limit")
            # Pinned 0.22.5 returns its structured NotExistsError as HTTP 400.
            # Only an exact optional read may interpret that sole error as
            # absence; never treat other errors or a rejected mutation so.
            if absent and response.status_code == 400:
                document = response.json()
                detail = document.get("detail") if isinstance(document, dict) else None
                if (
                    isinstance(detail, list)
                    and len(detail) == 1
                    and isinstance(detail[0], dict)
                    and detail[0].get("code") == "resource_not_exists"
                ):
                    return None
            _require(
                response.status_code < 300 and response.status_code >= 200,
                "dstack_api_rejected",
            )
            if not response.content:
                return {}
            return response.json()
        except DstackError:
            raise
        except Exception:
            raise DstackError("dstack_api_unavailable") from None

    def get(self, *, run_name=None, run_id=None):
        _require(bool(run_name) != bool(run_id), "dstack_exact_identity_required")
        if run_id:
            _uuid(run_id)
        else:
            _require(
                isinstance(run_name, str)
                and re.fullmatch(r"sixnine-[0-9a-f]{32}", run_name),
                "dstack_run_name_invalid",
            )
        return self._post(
            f"/api/project/{self.project}/runs/get",
            {"id": run_id} if run_id else {"run_name": run_name},
            absent=True,
        )

    def _provider_document(self, method, url, key, *, timeout, body=None, params=None):
        _require(
            (method, url)
            in {
                ("GET", VAST_INSTANCES_URL),
                ("POST", RUNPOD_GRAPHQL_URL),
                ("POST", "https://console.vast.ai/api/v0/bundles/"),
                ("GET", "https://console.vast.ai/api/v0/charges/"),
            }
            or method == "GET"
            and re.fullmatch(
                r"https://console\.vast\.ai/api/v0/instances/[1-9][0-9]{0,17}/", url
            ),
            "dstack_provider_inventory_unconfirmed",
        )
        if self._provider_session is None:
            self._provider_session = httpx.Client(
                follow_redirects=False, trust_env=False
            )

        def unique_object(pairs):
            value = {}
            for name, item in pairs:
                _require(name not in value, "dstack_provider_inventory_unconfirmed")
                value[name] = item
            return value

        def invalid_constant(_):
            raise DstackError("dstack_provider_inventory_unconfirmed")

        try:
            deadline = time.monotonic() + timeout
            for attempt in range(PROVIDER_READ_ATTEMPTS):
                remaining = deadline - time.monotonic()
                _require(remaining > 0, "dstack_provider_inventory_unavailable")
                with self._provider_session.stream(
                    method,
                    url,
                    headers={"Authorization": "Bearer " + key},
                    json=body,
                    params=params,
                    timeout=remaining,
                    follow_redirects=False,
                ) as response:
                    # These exact allowlisted endpoints are reads, including
                    # search/GraphQL POSTs. Mutations use _post and never retry.
                    if response.status_code == 429:
                        retry_after = _provider_retry_after(
                            response.headers.get("Retry-After")
                        )
                    else:
                        _require(
                            response.status_code == 200,
                            "dstack_provider_inventory_rejected",
                        )
                        content = bytearray()
                        for chunk in response.iter_bytes(chunk_size=65536):
                            _require(
                                time.monotonic() < deadline
                                and len(content) + len(chunk) <= 4 * 1024**2,
                                "dstack_provider_inventory_response_limit",
                            )
                            content.extend(chunk)
                        _require(
                            time.monotonic() < deadline,
                            "dstack_provider_inventory_unavailable",
                        )
                        return json.loads(
                            content,
                            object_pairs_hook=unique_object,
                            parse_constant=invalid_constant,
                        )
                # Close the refused response before sleeping. A long server
                # delay is surfaced rather than retried earlier than requested.
                delay = retry_after if retry_after is not None else 2**attempt
                if (
                    attempt + 1 == PROVIDER_READ_ATTEMPTS
                    or delay > PROVIDER_READ_MAX_DELAY
                ):
                    raise DstackReadRateLimited(retry_after)
                delay += random.uniform(0, 0.25)
                if delay >= deadline - time.monotonic():
                    raise DstackReadRateLimited(retry_after)
                time.sleep(delay)
        except DstackError:
            raise
        except Exception:
            raise DstackError("dstack_provider_inventory_unavailable") from None

    def _vast_key(self, *, timeout=None):
        # The exact existing dstack backend remains credential authority.
        # No registry/profile switch, key persistence or process env mutation.
        config = self._post(
            f"/api/project/{self.project}/backends/vastai/config_info",
            {},
            **({"timeout": timeout} if timeout is not None else {}),
        )
        creds = config.get("creds") if isinstance(config, dict) else None
        _require(
            isinstance(config, dict)
            and config.get("type") == "vastai"
            and isinstance(creds, dict)
            and creds.get("type") == "api_key",
            "dstack_host_account_unconfirmed",
        )
        key = creds.get("api_key")
        _require(
            isinstance(key, str)
            and 16 <= len(key) <= 4096
            and not any(c.isspace() for c in key),
            "dstack_host_account_unconfirmed",
        )
        return key

    def vast_offer_mapping(self, request, *, clock=time.time):
        """One search-only POST; mirror the installed gpuhunt 0.1.33 body.

        This is its direct filter JSON, not the distinct Vast CLI q wrapper.
        Unknown physical IDs are omitted; an offer ID is never a machine ID.
        Native planning still qualifies resources and its verified-first filter.
        """
        _require(request.backend == "vastai", "dstack_host_backend_invalid")
        observed = clock()
        query = {
            "direct_port_count": {"gte": 1},
            "reliability2": {"gte": request.min_reliability},
            "inet_down": {"gte": 1000},
            "verified": {"eq": True},
            "cuda_max_good": {"gte": 12.8},
            "compute_cap": {"gte": 600},
            "rentable": {"eq": True},
            "rented": {"eq": False},
            "num_gpus": {"eq": 1},
            "cpu_cores": {"gte": request.cpu_count},
            "cpu_ram": {"gte": vast_ram_floor(request)},
            "gpu_ram": {"gte": request.gpu_memory_gib * 1024 * 0.93},
            "disk_space": {"gte": request.disk_gib},
            "dph_total": {"lte": request.max_price_microusd / 1_000_000},
            "order": [["dph_base", "asc"]],
            "limit": 1000,
        }
        # Exact gpuhunt aliases; the normal backend plan does final GPU matching.
        names = []
        for name in request.gpu_names:
            if re.fullmatch(r"RTX[0-9]{4}[A-Za-z]?", name):
                names.append("RTX " + name[3:])
        if names and len(names) == len(request.gpu_names):
            query["gpu_name"] = {"in": names}
        document = self._provider_document(
            "POST",
            "https://console.vast.ai/api/v0/bundles/",
            self._vast_key(),
            timeout=min(self.timeout, 25),
            body=query,
        )
        _require(
            isinstance(document, dict)
            and isinstance(document.get("offers"), list)
            and len(document["offers"]) <= 1000,
            "dstack_host_offers_unconfirmed",
        )
        found = {}
        for row in document["offers"]:
            if not isinstance(row, dict):
                raise DstackError("dstack_host_offers_unconfirmed")
            offer, machine = row.get("id"), row.get("machine_id")
            if (
                type(offer) is not int
                or offer <= 0
                or type(machine) is not int
                or machine <= 0
            ):
                continue
            bandwidth = row.get("inet_down")
            if not (
                type(bandwidth) in (int, float)
                and math.isfinite(bandwidth)
                and 1000 <= bandwidth <= 1_000_000_000
            ):
                continue
            # Vast verification has appeared as a string as well as boolean.
            # Missing is unobserved, never projected as verified=False. The
            # exact verified-first search/native plan remains authoritative.
            verified = row.get("verified")
            if verified is False or verified in ("unverified", "deverified"):
                continue
            # The native Resources/JPD model omits advertised network speed.
            # Retain only bounded facts from this exact, frozen offer instead.
            location = row.get("geolocation")
            country, region = row.get("country"), None
            if isinstance(location, str) and 0 < len(location) <= 200:
                parts = location.replace(", ", ",").split(",")
                if len(parts) == 2:
                    city, advertised_country = parts
                    country = country if country is not None else advertised_country
                    location = advertised_country + "-" + city
                normalized = location.lower().replace(" ", "")
                if re.fullmatch(r"[a-z0-9-]{1,100}", normalized):
                    region = normalized
            if not isinstance(country, str) or not re.fullmatch(
                r"[A-Za-z][A-Za-z .'-]{0,99}", country
            ):
                country = None
            item = {
                "offer_id": str(offer),
                "machine_id": str(machine),
                "country": country,
                "region": region,
                "advertised_download_mbps": bandwidth,
            }
            _require(
                str(offer) not in found or found[str(offer)] == item,
                "dstack_host_offer_identity_conflict",
            )
            found[str(offer)] = item
        ended = clock()
        _require(
            type(observed) in (int, float)
            and math.isfinite(observed)
            and observed > 0
            and type(ended) in (int, float)
            and math.isfinite(ended)
            and 0 <= ended - observed < 120,
            "dstack_host_clock_invalid",
        )
        return {"observed_at": observed, "offers": list(found.values())}

    def vast_charges(self, start, end):
        from .invoice_checks import CHARGES_URL, read_vast_charges

        # This inspection also runs during active work. Keep its total I/O
        # on2s/6s request budgets, inside the30s freshness window; no thread
        # or second poller. An incomplete read stays unknown until next check.
        key = self._vast_key(timeout=2)
        return read_vast_charges(
            lambda params, timeout: self._provider_document(
                "GET",
                CHARGES_URL,
                key,
                params=params,
                timeout=min(timeout, self.timeout, 3),
            ),
            start,
            end,
            timeout_budget=6,
        )

    def vast_instance_identity(self, identity):
        _require(
            isinstance(identity, str) and re.fullmatch(r"[1-9][0-9]{0,17}", identity),
            "dstack_host_instance_invalid",
        )
        document = self._provider_document(
            "GET",
            f"https://console.vast.ai/api/v0/instances/{identity}/",
            self._vast_key(),
            timeout=min(self.timeout, 15),
        )
        _require(isinstance(document, dict), "dstack_host_instance_unconfirmed")
        row = document.get("instances")
        if row is None:
            return None
        _require(
            isinstance(row, dict)
            and type(row.get("id")) is int
            and str(row["id"]) == identity
            and type(row.get("machine_id")) is int
            and row["machine_id"] > 0,
            "dstack_host_instance_unconfirmed",
        )
        reliability = row.get("reliability2")
        return {
            "provider_instance_id": identity,
            "machine_id": str(row["machine_id"]),
            "reliability": reliability
            if type(reliability) in (int, float)
            and math.isfinite(reliability)
            and 0 <= reliability <= 1
            else None,
        }

    def reusable_hosts_confirmed(self, approved):
        """Complete project fleet read before apply; no unknown reuse fallback.

        Native 0.22.5 skips type constraints if an existing instance has no JPD.
        Refuse every potentially reusable unknown instance; a known matching
        bundle additionally needs an exact same-account physical identity read.
        """
        fleets = self._post(
            f"/api/project/{self.project}/fleets/list", {"include_imported": True}
        )
        _require(
            isinstance(fleets, list) and len(fleets) < 1000,
            "dstack_host_reuse_unconfirmed",
        )
        count = 0
        for fleet in fleets:
            _require(
                isinstance(fleet, dict) and isinstance(fleet.get("instances"), list),
                "dstack_host_reuse_unconfirmed",
            )
            for instance in fleet["instances"]:
                count += 1
                _require(
                    count <= 4096 and isinstance(instance, dict),
                    "dstack_host_reuse_unconfirmed",
                )
                status = instance.get("status")
                _require(
                    status
                    in {
                        "pending",
                        "provisioning",
                        "idle",
                        "busy",
                        "terminating",
                        "terminated",
                    },
                    "dstack_host_reuse_unconfirmed",
                )
                if status in {"terminated", "terminating"}:
                    continue
                # Public Instance assigns instance_type only from JPD. Public
                # DTOs omit the JPD/provider ID, so do not guess those fields.
                # With no type witness native filtering skips the selector.
                data = instance.get("instance_type")
                _require(
                    instance.get("backend") in BACKENDS
                    and isinstance(data, dict)
                    and isinstance(data.get("name"), str)
                    and data["name"],
                    "dstack_host_reuse_identity_unknown",
                )
                # Known nonmatching types are rejected again by native locked
                # assignment. Matching types use the same exact bundle-ID
                # selector; final allocation still needs a same-account
                # machine-ID read before any native bootstrap/readiness.
        return True

    def provider_inventory(self, backend, *, clock=None):
        """Transient complete account read using the SAME configured backend key.

        Pinned dstack's ProjectAdmin config_info read returns credentials only
        into this call's memory. They are never returned, stored, logged or sent
        to another origin. This is not stock, a rental command or reconciliation.
        Vast v1 pagination follows the current official show-instances contract;
        v0 list absence is deliberately unsupported. RunPod's public unpaginated
        User.pods query is distinct from its paginated podsConnection field.
        """
        _require(
            isinstance(backend, str) and backend in BACKENDS,
            "dstack_backend_unsupported",
        )
        clock = clock or time.time
        observed_at, started = clock(), time.monotonic()
        _require(
            type(observed_at) in (int, float)
            and math.isfinite(observed_at)
            and observed_at > 0,
            "dstack_capacity_clock_invalid",
        )
        try:
            config = self._post(
                f"/api/project/{self.project}/backends/{backend}/config_info", {}
            )
            creds = config.get("creds") if isinstance(config, dict) else None
            _require(
                isinstance(config, dict)
                and config.get("type") == backend
                and isinstance(creds, dict)
                and creds.get("type") == "api_key",
                "dstack_provider_inventory_unconfirmed",
            )
            key = creds.get("api_key")
            _require(
                isinstance(key, str)
                and 16 <= len(key) <= 4096
                and not any(char.isspace() for char in key),
                "dstack_provider_inventory_unconfirmed",
            )

            def read(method, url, *, body=None, params=None):
                remaining = 30 - (time.monotonic() - started)
                _require(remaining > 0, "dstack_provider_inventory_unavailable")
                return self._provider_document(
                    method,
                    url,
                    key,
                    body=body,
                    params=params,
                    timeout=min(15, self.timeout, remaining),
                )

            ids = []
            if backend == "vastai":
                params = {
                    "limit": 25,
                    "select_cols": '["id"]',
                    "order_by": '[{"col":"id","dir":"asc"}]',
                }
                cursors, total = set(), None
                for _ in range(32):
                    page = read("GET", VAST_INSTANCES_URL, params=params)
                    _require(
                        isinstance(page, dict)
                        and page.get("success") is True
                        and set(page)
                        <= {
                            "success",
                            "instances",
                            "instances_found",
                            "total_instances",
                            "next_token",
                            "label_counts",
                        },
                        "dstack_provider_inventory_unconfirmed",
                    )
                    rows, found, size = (
                        page.get("instances"),
                        page.get("instances_found"),
                        page.get("total_instances"),
                    )
                    _require(
                        isinstance(rows, list)
                        and type(found) is int
                        and found == len(rows)
                        and 0 <= found <= 25
                        and type(size) is int
                        and 0 <= size <= 800
                        and (total is None or total == size),
                        "dstack_provider_inventory_unconfirmed",
                    )
                    total = size
                    for row in rows:
                        _require(
                            isinstance(row, dict)
                            and type(row.get("id")) is int
                            and row["id"] > 0,
                            "dstack_provider_inventory_unconfirmed",
                        )
                        ids.append(str(row["id"]))
                    _require(
                        len(set(ids)) == len(ids) and len(ids) <= total,
                        "dstack_provider_inventory_unconfirmed",
                    )
                    _require(
                        "next_token" in page, "dstack_provider_inventory_unconfirmed"
                    )
                    cursor = page["next_token"]
                    if cursor is None:
                        _require(
                            len(ids) == total, "dstack_provider_inventory_unconfirmed"
                        )
                        break
                    _require(
                        found > 0
                        and isinstance(cursor, str)
                        and re.fullmatch(r"[A-Za-z0-9+/=_-]{1,4096}", cursor)
                        and cursor not in cursors,
                        "dstack_provider_inventory_unconfirmed",
                    )
                    cursors.add(cursor)
                    params["after_token"] = cursor
                else:
                    raise DstackError("dstack_provider_inventory_unconfirmed")
            else:
                document = read(
                    "POST",
                    RUNPOD_GRAPHQL_URL,
                    body={"query": "query { myself { pods { id } } }"},
                )
                _require(
                    isinstance(document, dict) and set(document) == {"data"},
                    "dstack_provider_inventory_unconfirmed",
                )
                data = document["data"]
                myself = data.get("myself") if isinstance(data, dict) else None
                _require(
                    isinstance(data, dict)
                    and set(data) == {"myself"}
                    and isinstance(myself, dict)
                    and set(myself) == {"pods"}
                    and isinstance(myself["pods"], list)
                    and len(myself["pods"]) <= 4096,
                    "dstack_provider_inventory_unconfirmed",
                )
                for row in myself["pods"]:
                    _require(
                        isinstance(row, dict)
                        and set(row) == {"id"}
                        and isinstance(row["id"], str)
                        and IDENTIFIER.fullmatch(row["id"]),
                        "dstack_provider_inventory_unconfirmed",
                    )
                    ids.append(row["id"])
                _require(
                    len(set(ids)) == len(ids), "dstack_provider_inventory_unconfirmed"
                )
            return {
                "backend": backend,
                "complete": True,
                "observed_at": observed_at,
                "instance_ids": ids,
            }
        except DstackError:
            raise
        except Exception:
            raise DstackError("dstack_provider_inventory_unavailable") from None

    def plan(self, spec):
        # Use the same fleet selection as apply. Global stock browsing can
        # offer machines that the configured native fleet cannot place (#659).
        return self._post(
            f"/api/project/{self.project}/runs/get_plan",
            {"run_spec": spec, "max_offers": 100, "for_offers_only": False},
        )

    def apply(self, spec):
        # Null expected resource + force:false is dstack's create-only CAS.
        return self._post(
            f"/api/project/{self.project}/runs/apply",
            {"plan": {"run_spec": spec, "current_resource": None}, "force": False},
        )

    def stop(self, run_name):
        _require(
            re.fullmatch(r"sixnine-[0-9a-f]{32}", run_name), "dstack_run_name_invalid"
        )
        return self._post(
            f"/api/project/{self.project}/runs/stop",
            {"runs_names": [run_name], "abort": False},
        )


def assert_run_owned(run, binding, project):
    """Compare the owned configuration subset, tolerating added server defaults."""
    _require(
        isinstance(run, dict) and run.get("project_name") == project,
        "dstack_run_ownership_mismatch",
    )
    _uuid(run.get("id"))
    spec = run.get("run_spec", {})
    wanted = binding["run_spec"]
    _require(isinstance(spec, dict), "dstack_run_binding_mismatch")
    _require(
        spec.get("run_name") == binding["run_name"], "dstack_run_ownership_mismatch"
    )
    actual = spec.get("configuration", {})
    expected = wanted["configuration"]
    _require(isinstance(actual, dict), "dstack_run_binding_mismatch")
    for key in (
        "type",
        "name",
        "image",
        "commands",
        "backends",
        "retry",
        "max_duration",
        "max_price",
        "tags",
    ):
        _require(actual.get(key) == expected[key], "dstack_run_binding_mismatch")
    _require(
        actual.get("env", {}) in ({}, None)
        and actual.get("ports", []) in ([], None)
        and actual.get("dstack", False) is False,
        "dstack_run_binding_mismatch",
    )
    _require(
        spec.get("ssh_key_pub") == wanted["ssh_key_pub"], "dstack_run_binding_mismatch"
    )
    if "profile" in wanted:
        profile = spec.get("profile")
        _require(
            isinstance(profile, dict)
            and profile.get("instance_types") == wanted["profile"]["instance_types"],
            "dstack_host_selector_changed",
        )
        _require(actual.get("instance_types") is None, "dstack_host_selector_changed")
        _require(
            all(
                value in (None, False, "", "default")
                for key, value in profile.items()
                if key not in {"instance_types", "name"}
            )
            and profile.get("name") in (None, "", "default"),
            "dstack_host_selector_changed",
        )
    for key in (
        "privileged",
        "docker",
        "registry_auth",
        "entrypoint",
        "schedule",
        "groups",
    ):
        _require(actual.get(key) in (None, False), "dstack_run_binding_mismatch")
    # 0.22.5 run preprocessing stores a homogeneous TaskConfiguration's
    # omitted nodes as integer 1. Preserve the frozen spec while allowing only
    # that single-node default, never multinode or grouped execution.
    nodes = actual.get("nodes")
    _require(
        nodes is None or type(nodes) is int and nodes == 1,
        "dstack_run_binding_mismatch",
    )
    for key in ("files", "repos", "setup", "volumes"):
        _require(actual.get(key) in (None, []), "dstack_run_binding_mismatch")
    _require(
        actual.get("spot_policy") == expected["spot_policy"]
        and actual.get("regions") == expected["regions"]
        and actual.get("stop_duration") == expected["stop_duration"],
        "dstack_run_binding_mismatch",
    )
    _require(
        _resources(actual.get("resources")) == _resources(expected["resources"]),
        "dstack_run_binding_mismatch",
    )
    wanted_options = expected.get("backend_options", [])
    actual_options = actual.get("backend_options") or []
    _require(
        isinstance(actual_options, list)
        and len(wanted_options) == len(actual_options)
        and all(
            isinstance(observed, dict)
            and all(observed.get(key) == value for key, value in desired.items())
            for observed, desired in zip(actual_options, wanted_options)
        ),
        "dstack_run_binding_mismatch",
    )
    if binding.get("run_id"):
        _require(run["id"] == binding["run_id"], "dstack_run_identity_changed")


# Compatibility for retained tests/callers; the public validator is authoritative.
_owns = assert_run_owned


def _range(value):
    # The pinned API serializes parsed scalar/range requirements as min/max.
    if isinstance(value, dict) and set(value) == {"min", "max"}:
        return value
    if type(value) in (int, float):
        return {"min": value, "max": value}
    if isinstance(value, str):
        match = re.fullmatch(r"([0-9]+)(?:GB)?\.\.", value)
        if match:
            return {"min": float(match.group(1)), "max": None}
    raise DstackError("dstack_resource_response_invalid")


def _resources(value):
    _require(isinstance(value, dict), "dstack_resource_response_invalid")
    cpu, gpu, disk = value.get("cpu", {}), value.get("gpu", {}), value.get("disk", {})
    _require(
        all(isinstance(item, dict) for item in (cpu, gpu, disk)),
        "dstack_resource_response_invalid",
    )
    return {
        "cpu_arch": cpu.get("arch"),
        "cpu_count": _range(cpu.get("count")),
        "memory": _range(value.get("memory")),
        "disk": _range(disk.get("size")),
        "gpu_vendor": gpu.get("vendor"),
        "gpu_name": gpu.get("name"),
        "gpu_count": _range(gpu.get("count")),
        "gpu_memory": _range(gpu.get("memory")),
    }


def terminal_no_allocation_shape(run, binding):
    """Complete failed singleton without runtime identity; not financial proof."""
    if (
        run.get("status") != "failed"
        or run.get("termination_reason") != "job_failed"
        or any(
            binding.get(key) is not None
            for key in (
                "provider_instance_id",
                "runtime_incarnation",
                "first_runtime_ready_at",
            )
        )
        or any(
            binding.get(key) not in (None, False)
            for key in (
                "bootstrap_started",
                "bootstrap_launch_started",
                "runtime_launch_started",
            )
        )
    ):
        return False
    jobs = run.get("jobs")
    if not isinstance(jobs, list) or len(jobs) != 1 or not isinstance(jobs[0], dict):
        return False
    submissions = jobs[0].get("job_submissions")
    latest = run.get("latest_job_submission")
    if (
        not isinstance(submissions, list)
        or len(submissions) != 1
        or not isinstance(latest, dict)
        or latest != submissions[0]
    ):
        return False
    try:
        _uuid(latest.get("id"))
    except DstackError:
        return False
    return (
        latest.get("status") == "failed"
        and latest.get("termination_reason") == "failed_to_start_due_to_no_capacity"
        and all(
            key in latest and latest[key] is None
            for key in ("job_provisioning_data", "job_runtime_data")
        )
    )


_terminal_no_allocation_shape = terminal_no_allocation_shape


def terminal_no_allocation(run, binding):
    from .dstack_allocation_proof import terminal_no_allocation as qualify

    return qualify(run, binding, shape_check=terminal_no_allocation_shape)


def live_capacity_usage(
    client, nodes, *, provider_inventory=None, clock=time.time, fresh_seconds=30
):
    """Read current owned dstack capacity without settling historical invoices.

    Call with original operator-node rows while holding the existing capacity
    reservation lock, and use the result for that same reservation transaction.
    Legacy rental nodes are outside this path. An unsubmitted intent reserves
    capacity immediately; absence, failed reads and identity drift keep it.

    The injected read-only provider reader must authenticate to the SAME account
    as the configured dstack backend. Its complete account list is projected as
    ``{backend, complete: True, observed_at, instance_ids}``; a stock list or
    partial/paginated response is not absence evidence. Read-start timestamps
    bound freshness, and the clock is sampled after the reads. No input rows,
    reservations, budgets, billing states or provider resources are modified.
    """
    _require(
        isinstance(nodes, (list, tuple)) and len(nodes) <= 4096,
        "dstack_capacity_rows_invalid",
    )
    _require(
        type(fresh_seconds) in (int, float)
        and math.isfinite(fresh_seconds)
        and 0 < fresh_seconds <= 120,
        "dstack_capacity_freshness_invalid",
    )
    inventories, counted, seen, resources, observations, projections, accounted = (
        {},
        [],
        set(),
        {},
        [],
        [],
        {},
    )
    hourly = 0

    def provider_absent(backend, identity):
        if backend not in inventories:
            try:
                document = (
                    provider_inventory(backend)
                    if callable(provider_inventory)
                    else None
                )
                _require(
                    isinstance(document, dict)
                    and document.get("backend") == backend
                    and document.get("complete") is True,
                    "dstack_provider_inventory_unconfirmed",
                )
                ids = document.get("instance_ids")
                _require(
                    isinstance(ids, (list, tuple))
                    and len(ids) <= 4096
                    and all(
                        isinstance(value, str) and IDENTIFIER.fullmatch(value)
                        for value in ids
                    )
                    and len(set(ids)) == len(ids),
                    "dstack_provider_inventory_unconfirmed",
                )
                observed = document.get("observed_at")
                _require(
                    type(observed) in (int, float)
                    and math.isfinite(observed)
                    and observed > 0,
                    "dstack_provider_inventory_unconfirmed",
                )
                inventories[backend] = (observed, frozenset(ids))
            except Exception:
                inventories[backend] = None
        inventory = inventories[backend]
        now = clock()
        _require(
            type(now) in (int, float) and math.isfinite(now) and now > 0,
            "dstack_capacity_clock_invalid",
        )
        return bool(
            inventory
            and 0 <= now - inventory[0] <= fresh_seconds
            and identity not in inventory[1]
        )

    for node in nodes:
        _require(
            isinstance(node, dict) or hasattr(node, "keys"),
            "dstack_capacity_rows_invalid",
        )
        payload = node.get("payload")
        if (
            not isinstance(payload, dict)
            or payload.get("capacity_backend") != "dstack-v1"
        ):
            continue
        intent_id = _uuid(node.get("intent_id"))
        _require(intent_id not in seen, "dstack_capacity_rows_invalid")
        seen.add(intent_id)
        cost = payload.get("hourly_cost_microusd")
        binding = payload.get("dstack")
        _require(
            isinstance(binding, dict) and binding.get("intent_id") == intent_id,
            "dstack_intent_binding_invalid",
        )
        included, reason, status = True, "dstack_capacity_pending", None
        resource_key = "intent:" + intent_id
        physical_gpus = 1
        if (
            node.get("desired_state") == "stopped"
            and binding.get("apply_started") is not True
            and not binding.get("run_id")
            and not binding.get("provider_instance_id")
            and binding.get("state") == "stopped"
            and binding.get("reason_code") == "dstack_never_applied"
            and binding.get("billing_state") == "no_charge_confirmed"
        ):
            included, reason = False, "dstack_never_applied"
        elif binding.get("apply_started") is True:
            try:
                run = (
                    client.get(run_id=binding["run_id"])
                    if binding.get("run_id")
                    else client.get(run_name=binding["run_name"])
                )
                assert_run_owned(run, binding, client.project)
                observed_status = run.get("status")
                _require(
                    isinstance(observed_status, str)
                    and observed_status in RUN_STATUSES,
                    "dstack_run_status_invalid",
                )
                status = observed_status
                reason = "dstack_capacity_active"
                if terminal_no_allocation(run, binding):
                    included, reason = False, "dstack_terminal_no_allocation"
                    projections.append(
                        (
                            intent_id,
                            cost,
                            resource_key,
                            physical_gpus,
                            included,
                            reason,
                            status,
                            binding.get("backend"),
                        )
                    )
                    continue
                if (
                    terminal_no_allocation_shape(run, binding)
                    and binding.get("host_observation") is None
                    and binding.get("host_failure") is None
                ):
                    backend = binding.get("backend")
                    _require(backend in BACKENDS, "dstack_backend_binding_mismatch")
                    # Fetch at most once for this backend. The final pass checks
                    # every current account ID against OTHER exact owned runs.
                    # An unknown PUT remains an unchanged accounting obligation.
                    provider_absent(backend, None)
                    reason = "dstack_terminal_provider_accounted"
                    projections.append(
                        (
                            intent_id,
                            cost,
                            resource_key,
                            physical_gpus,
                            included,
                            reason,
                            status,
                            backend,
                        )
                    )
                    continue
                provisioning = (run.get("latest_job_submission") or {}).get(
                    "job_provisioning_data"
                ) or {}
                identity = provisioning.get("instance_id")
                backend = binding.get("backend")
                _require(
                    backend in BACKENDS
                    and provisioning.get("backend") == backend
                    and isinstance(identity, str)
                    and IDENTIFIER.fullmatch(identity)
                    and binding.get("provider_instance_id") in (None, identity),
                    "dstack_instance_identity_unconfirmed",
                )
                resource_key = backend + ":" + identity
                gpus = (
                    provisioning.get("instance_type", {})
                    .get("resources", {})
                    .get("gpus")
                )
                if isinstance(gpus, list) and 1 <= len(gpus) <= 128:
                    physical_gpus = len(gpus)
                if status in TERMINAL:
                    if provider_absent(backend, identity):
                        included, reason = False, "dstack_terminal_provider_absent"
                    else:
                        reason = "dstack_terminal_provider_unconfirmed"
                accounted.setdefault(backend, {}).setdefault(identity, set()).add(
                    intent_id
                )
            except Exception:
                reason = "dstack_capacity_unconfirmed"
        projections.append(
            (
                intent_id,
                cost,
                resource_key,
                physical_gpus,
                included,
                reason,
                status,
                binding.get("backend"),
            )
        )
    # Later run/account reads can outlive an earlier positive absence receipt.
    # Revalidate every exclusion at the same final clock without another read.
    now = clock()
    _require(
        type(now) in (int, float) and math.isfinite(now) and now > 0,
        "dstack_capacity_clock_invalid",
    )
    for (
        intent_id,
        cost,
        resource_key,
        physical_gpus,
        included,
        reason,
        status,
        backend,
    ) in projections:
        if reason in {
            "dstack_terminal_provider_absent",
            "dstack_terminal_provider_accounted",
        }:
            inventory = inventories.get(backend)
            known = {
                identity
                for identity, owners in accounted.get(backend, {}).items()
                if len(owners) == 1 and intent_id not in owners
            }
            if (
                not inventory
                or not 0 <= now - inventory[0] <= fresh_seconds
                or reason == "dstack_terminal_provider_accounted"
                and not inventory[1].issubset(known)
            ):
                included, reason = True, "dstack_terminal_provider_unconfirmed"
            elif reason == "dstack_terminal_provider_accounted":
                included = False
        if included:
            _require(type(cost) is int and cost > 0, "dstack_active_cost_unconfirmed")
            counted.append(intent_id)
            # This adapter permits exactly one GPU per original run. Known
            # shared physical identities count once globally; unknown intents
            # retain independent conservative reservations.
            resources[resource_key] = max(resources.get(resource_key, 0), physical_gpus)
            hourly += cost
        observations.append(
            {
                "intent_id": intent_id,
                "counted": included,
                "reason_code": reason,
                "dstack_status": status,
            }
        )
    return {
        "instances": len(resources),
        "physical_gpus": sum(resources.values()),
        "logical_slots": len(counted),
        "hourly_cost_microusd": hourly,
        "active_instance_ids": tuple(counted),
        "resources": resources,
        "observations": observations,
    }


class DstackCapacity:
    def __init__(
        self,
        client: DstackClient,
        store: DstackStore,
        *,
        clock=time.time,
        readiness=None,
    ):
        self.client, self.store, self.clock, self.readiness = (
            client,
            store,
            clock,
            readiness,
        )
        configure = getattr(store, "set_host_client", None)
        if callable(configure):
            configure(client)

    def plan(self, request, ssh_key_pub):
        _require(
            request.hard_deadline is None or self.clock() < request.hard_deadline,
            "dstack_window_expired",
        )
        value = self.client.plan(request.run_spec(ssh_key_pub))
        _require(isinstance(value, dict), "dstack_plan_invalid")
        # Offers are advisory, not allocation guarantees or permission to rent.
        return value

    def start(self, request, ssh_key_pub):
        _require(
            request.hard_deadline is None or self.clock() < request.hard_deadline,
            "dstack_window_expired",
        )
        binding = request.binding(ssh_key_pub)
        prior = self.store.load(request.intent_id)
        _require(isinstance(prior, dict), "dstack_intent_missing")
        if prior.get("spec_digest"):
            _require(
                prior["spec_digest"] == binding["spec_digest"],
                "dstack_intent_binding_changed",
            )
        if prior.get("apply_started"):
            return self.observe(request.intent_id)
        # Exact pre-read refuses a collision; list absence is never proof.
        existing = self.client.get(run_name=request.run_name)
        _require(existing is None, "dstack_existing_run_requires_reconciliation")
        if not self.store.begin_apply(request.intent_id, binding):
            return self.observe(request.intent_id)
        try:
            run = self.client.apply(binding["run_spec"])
            assert_run_owned(run, binding, self.client.project)
            self.store.record(
                request.intent_id,
                {
                    "run_id": run["id"],
                    "state": "starting",
                    "observed_at": self.clock(),
                    "reason_code": "dstack_runtime_not_observed",
                },
            )
        except DstackError:
            self.store.record(
                request.intent_id,
                {
                    "state": "creation_unknown",
                    "observed_at": self.clock(),
                    "reason_code": "dstack_apply_requires_reconciliation",
                },
            )
        return self.observe(request.intent_id)

    def observe_terminal(self, intent_id):
        """Refresh only an exact owned terminal run, without native activity.

        Continuing funding cannot reopen a draining historical intent. Its
        terminal upstream state can still be projected into the original row.
        Nonterminal, absent, failed or changed-identity reads grant no evidence
        and do not alter that row; ordinary funded observation remains separate.
        """
        binding = self.store.load(intent_id)
        _require(
            isinstance(binding, dict) and binding.get("apply_started") is True,
            "dstack_intent_not_submitted",
        )
        try:
            run = (
                self.client.get(run_id=binding["run_id"])
                if binding.get("run_id")
                else self.client.get(run_name=binding["run_name"])
            )
            if run is None:
                return None
            assert_run_owned(run, binding, self.client.project)
            if run.get("status") not in TERMINAL:
                return None
            value = self._observation(run, binding)
            self._confirm_provider_removal(run, binding, value)
        except Exception:
            return None
        self.store.record(intent_id, value)
        return value

    def review_unallocated_start(self, intent_id, *, reviewed_by, source):
        """Trusted one-shot owner review of an older unallocated failed start.

        This is not an HTTP/automatic settlement rule. The operator supplies
        the explicit owner confirmation; current exact-run and complete empty
        same-account inventory are independently re-read before ledger commit.
        """
        _require(
            isinstance(reviewed_by, str)
            and IDENTIFIER.fullmatch(reviewed_by)
            and isinstance(source, str)
            and re.fullmatch(
                r"https://github\.com/inkseq/h3-studio/issues/[1-9][0-9]*(?:#issuecomment-[0-9]+)?",
                source,
            ),
            "dstack_no_allocation_review_invalid",
        )
        binding = self.store.load(intent_id)
        _require(
            binding.get("backend") == "vastai" and binding.get("run_id"),
            "dstack_no_allocation_review_unconfirmed",
        )
        run = self.client.get(run_id=binding["run_id"])
        assert_run_owned(run, binding, self.client.project)
        _require(
            terminal_no_allocation(run, binding),
            "dstack_no_allocation_review_unconfirmed",
        )
        inventory = self.client.provider_inventory(binding["backend"])
        at = self.clock()
        _require(
            isinstance(inventory, dict)
            and inventory.get("backend") == binding["backend"]
            and inventory.get("complete") is True
            and inventory.get("instance_ids") == []
            and type(inventory.get("observed_at")) in (int, float)
            and 0 <= at - inventory["observed_at"] <= 30,
            "dstack_no_allocation_review_unconfirmed",
        )
        latest = run["latest_job_submission"]
        message = latest.get("termination_reason_message")
        _require(
            isinstance(message, str) and 0 < len(message.encode()) <= 8192,
            "dstack_no_allocation_review_unconfirmed",
        )
        review = {
            "version": 1,
            "source": source,
            "reviewed_by": reviewed_by,
            "owner_confirmation": "no_machine_allocated",
            "run_id": run["id"],
            "submission_id": latest["id"],
            "message_sha256": hashlib.sha256(message.encode()).hexdigest(),
            "observed_at": at,
            "inventory_observed_at": inventory["observed_at"],
            "provider": binding["backend"],
            "complete_account_empty": True,
        }
        return self.store.review_unallocated_start(intent_id, review)

    def observe(self, intent_id, *, _on_observed=None):
        binding = self.store.load(intent_id)
        _require(
            isinstance(binding, dict) and binding.get("apply_started") is True,
            "dstack_intent_not_submitted",
        )
        observed_run = None
        try:
            run = (
                self.client.get(run_id=binding["run_id"])
                if binding.get("run_id")
                else self.client.get(run_name=binding["run_name"])
            )
            if run is None:
                value = {
                    "state": "creation_unknown"
                    if not binding.get("stop_started")
                    else "removal_unknown",
                    "ready": False,
                    "reason_code": "dstack_exact_run_absent",
                    "observed_at": self.clock(),
                }
            else:
                assert_run_owned(run, binding, self.client.project)
                value = self._observation(run, binding)
                self._confirm_provider_removal(run, binding, value)
                observed_run = run
        except DstackError as error:
            value = {
                "state": "observation_unknown",
                "ready": False,
                "reason_code": str(error),
                "observed_at": self.clock(),
            }
        except Exception:
            value = {
                "state": "observation_unknown",
                "ready": False,
                "reason_code": "dstack_response_invalid",
                "observed_at": self.clock(),
            }
        self.store.record(intent_id, value)
        # A controller may reuse this exact read for scheduling only. The raw
        # run never enters the ledger or changes the original observation.
        if observed_run is not None and _on_observed is not None:
            _on_observed(observed_run)
        return value

    def _confirm_provider_removal(self, run, binding, value):
        # Native terminal state alone is not physical removal. Consult
        # the existing credential-bound complete provider inventory only
        # for a known original instance, never an unknown allocation.
        identity = binding.get("provider_instance_id")
        if (
            value.get("state") == "stopped"
            and identity
            and not binding.get("provider_removal_proof")
        ):
            try:
                inventory = self.client.provider_inventory(binding["backend"])
                ids = inventory.get("instance_ids")
                at = self.clock()
                if (
                    inventory.get("backend") == binding["backend"]
                    and inventory.get("complete") is True
                    and isinstance(ids, list)
                    and len(ids) <= 4096
                    and all(isinstance(i, str) and IDENTIFIER.fullmatch(i) for i in ids)
                    and len(set(ids)) == len(ids)
                    and identity not in ids
                    and type(inventory.get("observed_at")) in (int, float)
                    and math.isfinite(inventory["observed_at"])
                    and 0 <= at - inventory["observed_at"] <= 30
                ):
                    value["provider_removal_proof"] = {
                        "version": 1,
                        "backend": binding["backend"],
                        "run_id": run["id"],
                        "provider_instance_id": identity,
                        "observed_at": at,
                        "inventory_observed_at": inventory["observed_at"],
                        "complete": True,
                        "absent": True,
                        "inventory_sha256": hashlib.sha256(
                            json.dumps(sorted(ids), separators=(",", ":")).encode()
                        ).hexdigest(),
                    }
            except Exception:
                pass  # Failed reads never become absence or free billing.

    def _observation(self, run, binding):
        status = run.get("status")
        _require(
            isinstance(status, str) and status in RUN_STATUSES,
            "dstack_run_status_invalid",
        )
        value = {
            "run_id": run["id"],
            "run_name": binding["run_name"],
            "dstack_status": status,
            "state": "starting",
            "ready": False,
            "model_load_state": "not_observed",
            "billing_state": "unsettled",
            "observed_at": self.clock(),
            "reason_code": "dstack_runtime_not_observed",
        }
        submission = run.get("latest_job_submission") or {}
        _require(isinstance(submission, dict), "dstack_response_invalid")
        image = image_preparation(submission)
        preparation_phase = (
            "image_pull" if submission.get("status") == "pulling" else "provisioning"
        )
        value["preparation"] = {
            "phase": preparation_phase,
            "phase_started_at": None,
            "updated_at": value["observed_at"],
            "download": None,
            "image": image,
            "model": None,
        }
        # The library's modeled cost is an estimate, not a supplier invoice.
        cost = run.get("cost")
        if type(cost) in (int, float) and math.isfinite(cost) and cost >= 0:
            value["estimated_cost_microusd"] = round(cost * 1_000_000)
        # Allocation evidence may exist before RUNNING (or survive terminal
        # failure). Never postpone its capture until a successful bootstrap.
        provisioning = submission.get("job_provisioning_data")
        if isinstance(provisioning, dict) and provisioning:
            try:
                physical = self._host_observation(
                    provisioning, binding, value["observed_at"]
                )
            except DstackError:
                # Physical telemetry cannot erase an exact owned upstream
                # terminal state when the supplier has already removed it.
                # This grants no host identity, readiness, removal or billing
                # proof; live-capacity exclusion still needs fresh complete
                # provider absence through its original independent guard.
                if status not in TERMINAL:
                    raise
                physical = None
            if physical is not None:
                value.update(
                    host_observation=physical,
                    provider_instance_id=physical["provider_instance_id"],
                )
            if (
                physical is not None
                and status == "failed"
                and not binding.get("stop_started")
                and not binding.get("first_runtime_ready_at")
                and submission.get("termination_reason")
                == "waiting_runner_limit_exceeded"
            ):
                value["host_failure"] = {
                    "version": 1,
                    "stage": "runtime_start",
                    "reason_code": "provider_runner_timeout",
                    "at": value["observed_at"],
                    "qualified_start_failure": True,
                }
        if status in TERMINAL:
            value.update(
                state="stopped", reason_code="dstack_run_terminal_billing_pending"
            )
            message = submission.get("termination_reason_message")
            if (
                binding.get("backend") == "runpod"
                and isinstance(message, str)
                and message.startswith("sixnine_runpod_outcome_v1:")
            ):
                # The original create may have allocated. A terminal native job
                # is not provider absence, a free slot, or permission to reapply.
                value.update(
                    state="creation_unknown",
                    reason_code="dstack_allocation_requires_reconciliation",
                )
                return value
            if isinstance(message, str) and message.startswith(
                "sixnine_vast_attempts_v1:"
            ):
                from .dstack_allocation_proof import from_terminal_run

                if from_terminal_run(run, binding) is None:
                    value.update(
                        state="creation_unknown",
                        reason_code="dstack_allocation_requires_reconciliation",
                    )
                    return value
            if terminal_no_allocation(run, binding):
                value["reason_code"] = "dstack_no_capacity_billing_pending"
                from .dstack_allocation_proof import from_terminal_run

                proof = from_terminal_run(run, binding)
                if proof is not None:
                    value["no_allocation_proof"] = proof
                    value.update(
                        billing_state="no_charge_confirmed",
                        reason_code="dstack_never_allocated",
                    )
            value["preparation"]["phase"] = "stopped"
            return value
        if status in {"terminating", "stopping"}:
            value.update(state="stopping", reason_code="dstack_stop_pending")
            value["preparation"]["phase"] = "stopping"
            return value
        if status != "running":
            if binding.get("stop_started"):
                value.update(state="stopping", reason_code="dstack_stop_pending")
                value["preparation"]["phase"] = "stopping"
            return value
        provisioning = submission.get("job_provisioning_data") or {}
        _require(isinstance(provisioning, dict), "dstack_response_invalid")
        _require(
            provisioning.get("backend") == binding["backend"],
            "dstack_backend_binding_mismatch",
        )
        resource = provisioning.get("instance_type", {}).get("resources", {})
        _require(
            isinstance(resource, dict)
            and isinstance(resource.get("gpus"), list)
            and len(resource["gpus"]) == 1,
            "dstack_allocated_gpu_count_unconfirmed",
        )
        instance = provisioning.get("instance_id")
        _require(
            isinstance(instance, str)
            and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}", instance),
            "dstack_instance_identity_unconfirmed",
        )
        if binding.get("provider_instance_id"):
            _require(
                instance == binding["provider_instance_id"],
                "dstack_instance_identity_changed",
            )
        value["provider_instance_id"] = instance
        if binding.get("stop_started"):
            # The original stop already fenced/retired inference. Keep fresh
            # provider identity evidence without bootstrapping/reopening it.
            value.update(state="stopping", reason_code=self._stop_retry_reason(binding))
            value["preparation"]["phase"] = "stopping"
            return value
        value["state"] = "runtime_unconfirmed"
        if self.readiness is None:
            return value
        # Cold bootstrap needs the exact provider identity journaled first.
        # A warm check must not transiently revoke its original qualified
        # proof: concurrent admission may use it only until its existing
        # timestamp expires. Neither a pending check nor its later success
        # moves this provider-read timestamp forward.
        incarnation = binding.get("runtime_incarnation")
        observed = binding.get("observed_at")
        warm = (
            binding.get("state") in {"ready", "busy"}
            and binding.get("dstack_status") == "running"
            and binding.get("run_id") == run["id"]
            and binding.get("provider_instance_id") == instance
            and isinstance(incarnation, str)
            and re.fullmatch(r"[0-9a-f]{32}", incarnation)
            and type(observed) in (int, float)
            and math.isfinite(observed)
            and 0 <= observed <= value["observed_at"]
        )
        if not warm:
            provider_observation = copy.deepcopy(value)
            # This identity journal precedes the native read, not a fresh native
            # phase transition. Do not reset an already observed download timer.
            provider_observation.pop("preparation", None)
            self.store.record(binding["intent_id"], provider_observation)
            binding = self.store.load(binding["intent_id"])
        try:
            native = self.readiness(copy.deepcopy(binding), copy.deepcopy(run))
        except DstackError as error:
            # Native setup reports normal cold-start progress through this
            # exact static code. Do not expose arbitrary callback text, or
            # infer readiness from the preparation observation.
            phases = {
                "dstack_runtime_preparing": "provisioning",
                "dstack_runtime_downloading_weights": "model_download",
                "dstack_runtime_starting_runtime": "runtime_start",
                "dstack_runtime_loading_model": "model_load",
            }
            code = (
                error.args[0]
                if len(error.args) == 1 and isinstance(error.args[0], str)
                else None
            )
            value["reason_code"] = (
                code
                if code in NATIVE_REASON_CODES
                else "dstack_native_observation_failed"
            )
            preparation = native_preparation(getattr(error, "preparation", None))
            if preparation is None:
                preparation = native_preparation(binding.get("preparation"))
            if preparation is not None:
                value["preparation"] = {**preparation, "image": image}
                if preparation.get("reason_code"):
                    value["reason_code"] = preparation["reason_code"]
            if code in phases:
                value["bootstrap_phase"] = phases[code]
            return value
        except Exception:
            value["reason_code"] = "dstack_native_observation_failed"
            return value
        _require(
            isinstance(native, HostReadiness)
            and native.slot_key == binding["intent_id"]
            and native.manifest_digest == binding["manifest_digest"]
            and type(native.idle) is bool
            and isinstance(native.incarnation, str)
            and re.fullmatch(r"[0-9a-f]{32}", native.incarnation),
            "dstack_native_binding_mismatch",
        )
        if binding.get("runtime_incarnation"):
            _require(
                native.incarnation == binding["runtime_incarnation"],
                "dstack_native_incarnation_changed",
            )
        value.update(
            state="ready" if native.idle else "busy",
            ready=native.idle,
            runtime_incarnation=native.incarnation,
            bootstrap_phase="runtime_ready",
            reason_code="dstack_runtime_ready_model_load_unobserved"
            if native.idle
            else "dstack_runtime_busy",
        )
        preparation = native_preparation(native.preparation)
        if preparation is None:
            preparation = {
                "phase": "runtime_ready",
                "phase_started_at": None,
                "updated_at": value["observed_at"],
                "download": None,
                "model": None,
            }
        model = preparation.get("model")
        if model is not None:
            value["model_load_state"] = model["state"]
            if model["state"] == "loading":
                preparation["phase"] = "model_load"
                preparation["phase_started_at"] = model.get("phase_started_at")
                preparation["updated_at"] = model.get("updated_at")
            elif model["state"] == "loaded" and native.idle:
                value["reason_code"] = "dstack_runtime_ready"
            if model.get("reason_code"):
                preparation["reason_code"] = model["reason_code"]
                value["reason_code"] = model["reason_code"]
        value["preparation"] = {**preparation, "image": image}
        return value

    def _host_observation(self, provisioning, binding, now):
        """Original JPD resources plus one same-account physical identity.

        An expected/selected offer is not an allocated host. Old unknown runs
        remain compatible; new enforced selectors fail closed before readiness.
        """
        if binding.get("backend") != "vastai":
            return None
        strict = binding.get("host_selection") is not None
        try:
            _require(
                provisioning.get("backend") == "vastai",
                "dstack_host_allocation_unconfirmed",
            )
            instance = provisioning.get("instance_id")
            kind = provisioning.get("instance_type")
            _require(
                isinstance(instance, str)
                and re.fullmatch(r"[1-9][0-9]{0,17}", instance)
                and isinstance(kind, dict)
                and isinstance(kind.get("name"), str)
                and re.fullmatch(r"[1-9][0-9]{0,17}", kind["name"]),
                "dstack_host_allocation_unconfirmed",
            )
            prior = binding.get("host_observation")
            if prior is not None:
                from .host_policy import observation

                prior = observation(prior, now=now)
                _require(
                    prior["provider_instance_id"] == instance
                    and prior["offer_id"] == kind["name"],
                    "dstack_host_allocation_changed",
                )
                return prior
            reader = getattr(self.client, "vast_instance_identity", None)
            _require(callable(reader), "dstack_host_allocation_unconfirmed")
            identity = reader(instance)
            _require(
                isinstance(identity, dict)
                and identity.get("provider_instance_id") == instance,
                "dstack_host_allocation_unconfirmed",
            )
            if strict:
                approved = {
                    r["offer_id"]: r for r in binding["host_selection"]["offers"]
                }
                _require(
                    approved.get(kind["name"], {}).get("machine_id")
                    == identity.get("machine_id"),
                    "dstack_host_allocation_changed",
                )
            resource = kind.get("resources", {})
            gpus = resource.get("gpus")
            _require(
                isinstance(gpus, list) and len(gpus) == 1 and isinstance(gpus[0], dict),
                "dstack_host_allocation_unconfirmed",
            )
            price = provisioning.get("price")
            _require(
                type(price) in (int, float) and math.isfinite(price) and price > 0,
                "dstack_host_allocation_unconfirmed",
            )
            _require(
                not strict
                or price <= binding["run_spec"]["configuration"]["max_price"],
                "dstack_host_allocation_unconfirmed",
            )
            value = {
                "version": 1,
                "provider": "vastai",
                "machine_id": identity["machine_id"],
                "provider_instance_id": instance,
                "offer_id": kind["name"],
                "observed_at": now,
                "region": provisioning.get("region"),
                "gpu": gpus[0].get("name"),
                "gpu_count": 1,
                "allocated_ram_mib": resource.get("memory_mib"),
                "allocated_vram_mib": gpus[0].get("memory_mib"),
                "cpu": resource.get("cpus"),
                "disk_mib": resource.get("disk", {}).get("size_mib"),
                "reliability": identity.get("reliability"),
                "quoted_hourly_microusd": math.ceil(price * 1_000_000),
                "identity_confirmed": True,
                "physical_id_source": "vast_machine_id",
            }
            if strict:
                from .host_policy import offer_metadata

                advertised = offer_metadata(
                    {
                        key: approved[kind["name"]][key]
                        for key in ("country", "region", "advertised_download_mbps")
                        if key in approved[kind["name"]]
                    }
                )
                value.update(
                    country=advertised["country"],
                    advertised_download_mbps=advertised["advertised_download_mbps"],
                )
            from .host_policy import observation

            return observation(value, now=now)
        except Exception:
            if strict:
                raise DstackError("dstack_host_allocation_unconfirmed") from None
            return None

    def _stop_retry_reason(self, binding):
        count, due = (
            binding.get("stop_attempt_count"),
            binding.get("stop_next_retry_at"),
        )
        if (
            type(count) is not int
            or count < 1
            or type(due) not in (int, float)
            or not math.isfinite(due)
            or due <= 0
        ):
            return "dstack_stop_retry_journal_unconfirmed"
        if count >= 5:
            return "dstack_stop_retry_exhausted"
        return (
            "dstack_stop_retry_waiting"
            if self.clock() < due
            else "dstack_stop_retry_due"
        )

    def stop(self, intent_id):
        binding = self.store.load(intent_id)
        _require(
            isinstance(binding, dict) and binding.get("run_id"),
            "dstack_owned_run_id_required",
        )
        if binding.get("stop_started"):
            # dstack 0.22.5 stop_runs only moves existing nonfinished runs to
            # TERMINATING under locks; repeating that stop never creates a run.
            # Source: dstack/_internal/server/services/runs/__init__.py:868-909
            # in upstream tag 0.22.5 (not the create/update apply endpoint).
            # An accepted lost response normally already reads TERMINATING.
            # Only a *new* exact-owned positive RUNNING read can retry a stop
            # that was never delivered, after the original ledger's due/count
            # CAS. Cached RUNNING, absent/unknown and terminal reads cannot.
            observation = self.observe(intent_id)
            if (
                observation.get("dstack_status") != "running"
                or observation.get("run_id") != binding["run_id"]
                or not binding.get("provider_instance_id")
                or observation.get("provider_instance_id")
                != binding["provider_instance_id"]
            ):
                return observation
            binding = self.store.load(intent_id)
            reason = self._stop_retry_reason(binding)
            if reason != "dstack_stop_retry_due":
                return {**observation, "reason_code": reason}
            # observe() has committed the fresh exact identity. This gate must
            # recheck original jobs, worker retirement and the retry journal
            # atomically; process-local counters do not survive restart.
            if not self.store.begin_stop(intent_id, binding["run_id"], retry=True):
                reason = self._stop_retry_reason(self.store.load(intent_id))
                return {
                    **observation,
                    "reason_code": reason
                    if reason != "dstack_stop_retry_due"
                    else "dstack_business_obligations_pending",
                }
        else:
            run = self.client.get(run_id=binding["run_id"])
            _require(run is not None, "dstack_stop_identity_unconfirmed")
            assert_run_owned(run, binding, self.client.project)
            if run.get("status") in TERMINAL | {"terminating", "stopping"}:
                value = self._observation(run, binding)
                self.store.record(intent_id, value)
                return value
            if not self.store.begin_stop(intent_id, binding["run_id"]):
                return {
                    "state": "draining",
                    "ready": False,
                    "reason_code": "dstack_business_obligations_pending",
                    "observed_at": self.clock(),
                }
        try:
            self.client.stop(binding["run_name"])
            value = {
                "state": "stopping",
                "ready": False,
                "reason_code": "dstack_stop_pending",
                "observed_at": self.clock(),
            }
        except DstackError:
            value = {
                "state": "removal_unknown",
                "ready": False,
                "reason_code": "dstack_stop_requires_reconciliation",
                "observed_at": self.clock(),
            }
        self.store.record(intent_id, value)
        return value


def idle_action(
    *,
    now,
    hard_deadline,
    hold_until,
    last_business_activity,
    idle_seconds,
    active_jobs,
    unsafe_attempts,
    collection_holds,
    observations_fresh,
    lifecycle_version=1,
    keep_on="idle",
    budget_available=True,
):
    """Pure policy; stopping is still guarded transactionally by begin_stop.

    A running sleep command is not business-idle. Finite version-1 rentals
    retain their original absolute lifetime. Explicit version 2 uses business
    idle/hold policy and the original ledger's bounded funding authority.
    """
    _require(
        all(
            type(v) in (int, float) and math.isfinite(v) and v >= 0
            for v in (now, hold_until, last_business_activity, idle_seconds)
        ),
        "dstack_idle_policy_invalid",
    )
    _require(
        idle_seconds > 0
        and all(
            type(n) is int and n >= 0
            for n in (active_jobs, unsafe_attempts, collection_holds)
        )
        and type(observations_fresh) is bool,
        "dstack_idle_policy_invalid",
    )
    _require(
        type(lifecycle_version) is int
        and lifecycle_version in {1, 2}
        and type(budget_available) is bool
        and isinstance(keep_on, str)
        and keep_on in {"idle", "until", "until_stopped"},
        "dstack_idle_policy_invalid",
    )
    if lifecycle_version == 1:
        _require(
            type(hard_deadline) in (int, float)
            and math.isfinite(hard_deadline)
            and hard_deadline >= 0
            and keep_on == "idle"
            and budget_available,
            "dstack_idle_policy_invalid",
        )
    else:
        _require(hard_deadline is None, "dstack_idle_policy_invalid")
    if not observations_fresh:
        return "reconcile"
    if lifecycle_version == 2:
        obligations = active_jobs or unsafe_attempts or collection_holds
        if not budget_available:
            return "drain" if obligations else "stop"
        if obligations or keep_on == "until_stopped" or now < hold_until:
            return "retain"
        # Expiring a hold begins a full idle interval; it must not immediately
        # stop a machine whose previous activity predates the hold.
        last_activity = max(last_business_activity, hold_until)
        return "stop" if now - last_activity >= idle_seconds else "retain"
    if active_jobs or unsafe_attempts or collection_holds:
        return "drain" if now >= hard_deadline else "retain"
    if now >= hard_deadline:
        return "stop"
    if now < hold_until:
        return "retain"
    return "stop" if now - last_business_activity >= idle_seconds else "retain"


def native_bootstrap_config(
    request,
    *,
    source_bundle_sha256,
    prepared_root="/opt/workspace-internal/Wan2GP",
    model_root="/root/sixnine-cache/models",
):
    """Render the unchanged native bootstrap interface, without any credentials.

    The CPU controller must upload its exact bound bundle/manifest and a unique
    private runtime token, commit bootstrap intent, then invoke the original
    one-shot bootstrap once. A timeout re-observes that original operation.
    Source and environment checks remain in wangp_profile_bootstrap.prepare.
    """
    _require(
        isinstance(source_bundle_sha256, str)
        and SHA256.fullmatch(source_bundle_sha256),
        "dstack_source_bundle_digest_invalid",
    )
    for path in (prepared_root, model_root):
        _require(
            isinstance(path, str)
            and PurePosixPath(path).is_absolute()
            and not any(part in {"..", "."} for part in PurePosixPath(path).parts)
            and re.fullmatch(r"/[A-Za-z0-9_./-]+", path),
            "dstack_runtime_path_invalid",
        )
    base = "/workspace/h3-studio/profile-slot-0"
    return {
        "version": 1,
        "install_root": "/root/sixnine-cache/operator/profile-slot-0",
        "source_bundle_path": base + "/wangp-package.tar.gz",
        "source_bundle_sha256": source_bundle_sha256,
        "dependency_artifact_url": "",
        "dependency_artifact_path": "",
        "dependency_artifact_sha256": "0" * 64,
        "manifest_path": base + "/wangp-manifest.json",
        "model_root": model_root,
        "config_path": "/root/sixnine-cache/operator/profile-slot-0/wgp_config.json",
        "status_path": base + "/setup-status.json",
        "port": 8199,
        "prepared_root": prepared_root,
        "deployment_profile_id": request.profile_id,
        "profile_slot_index": 0,
        "expected_host_gpus": 1,
    }


def main(argv=None):
    """Trusted CPU operations CLI. Persistence is the original service factory."""
    parser = argparse.ArgumentParser(
        description="Sixnine dstack capacity operations (0.22.5)"
    )
    parser.add_argument(
        "action",
        choices=("spec", "bootstrap-config", "plan", "start", "observe", "stop"),
    )
    parser.add_argument(
        "--request",
        type=Path,
        required=True,
        help="Frozen, non-secret CapacityRequest JSON",
    )
    parser.add_argument("--ssh-public-key", type=Path)
    parser.add_argument("--source-bundle-sha256")
    parser.add_argument(
        "--factory",
        help="Existing-ledger studio_platform.module:factory; takes no arguments",
    )
    args = parser.parse_args(argv)
    try:
        _require(
            args.request.is_absolute()
            and not args.request.is_symlink()
            and args.request.stat().st_size <= 16384,
            "dstack_request_file_invalid",
        )
        values = json.loads(args.request.read_text(encoding="utf-8"))
        _require(isinstance(values, dict), "dstack_request_file_invalid")
        for key in ("gpu_names", "regions"):
            if key in values:
                _require(isinstance(values[key], list), "dstack_request_file_invalid")
                values[key] = tuple(values[key])
        request = CapacityRequest(**values)
        public_key = None
        if args.action in {"spec", "plan", "start"}:
            _require(
                args.ssh_public_key is not None
                and args.ssh_public_key.is_absolute()
                and not args.ssh_public_key.is_symlink()
                and args.ssh_public_key.stat().st_size <= 16384,
                "dstack_public_ssh_key_invalid",
            )
            public_key = args.ssh_public_key.read_text(encoding="utf-8").strip()
        if args.action == "spec":
            result = request.run_spec(public_key)
        elif args.action == "bootstrap-config":
            result = native_bootstrap_config(
                request, source_bundle_sha256=args.source_bundle_sha256
            )
        else:
            _require(
                isinstance(args.factory, str)
                and re.fullmatch(
                    r"studio_platform\.[A-Za-z0-9_]+:[A-Za-z0-9_]+", args.factory
                ),
                "dstack_existing_store_factory_required",
            )
            module, function = args.factory.split(":")
            capacity = getattr(importlib.import_module(module), function)()
            _require(
                isinstance(capacity, DstackCapacity), "dstack_capacity_factory_invalid"
            )
            if args.action == "plan":
                plan = capacity.plan(request, public_key)
                plans = plan.get("job_plans", [])
                result = {
                    "run_name": request.run_name,
                    "backend": request.backend,
                    "profile_id": request.profile_id,
                    "mode": request.mode,
                    "total_offers": sum(p.get("total_offers", 0) for p in plans),
                    "current_resource_exists": plan.get("current_resource") is not None,
                    "creation_started": False,
                }
            elif args.action == "start":
                result = capacity.start(request, public_key)
            else:
                result = getattr(capacity, args.action)(request.intent_id)
        print(json.dumps(result, sort_keys=True, allow_nan=False))
        return 0
    except DstackError as error:
        print(json.dumps({"state": "rejected", "reason_code": str(error)}))
    except Exception:
        print(json.dumps({"state": "rejected", "reason_code": "dstack_cli_failed"}))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
