"""Disabled-by-default Boyesir transport contract, NOT a registered worker backend.

No import/constructor credential loads or network calls. A trusted shared-ledger
gate and media authorizer must be supplied; this module never invents a second
attempt database. Public model declarations are not successful inference evidence.
"""

from __future__ import annotations

from contextlib import closing, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
import hashlib
import hmac
import importlib
import ipaddress
import json
import logging
import math
import os
from pathlib import Path
import re
import stat
import sys
import threading
import time
from typing import Protocol
from urllib.parse import urlsplit
import uuid

import httpx

from .worker import (
    BackendError,
    NotReady,
    Outcome,
    SubmissionRejected,
    SubmissionUncertain,
)

BASE_URL = "https://boyesir.com"
SERVICE = "boyesir"
PROFILE = "boyesir--boyesir-windows-dpapi"
RESULT_HOSTS = frozenset({"boyesir.com", "gf.boyesir.com", "hub.boyesir.com"})
ID = re.compile(r"[A-Za-z0-9_-]{1,200}\Z")
SHA = re.compile(r"[0-9a-f]{64}\Z")
MIB = 1024**2
_PRIVATE_TRACE = ContextVar("boyesir_private_transport_trace", default=False)
_TRACE_LOGGERS = (
    "httpx",
    "httpcore.connection",
    "httpcore.http11",
    "httpcore.http2",
    "httpcore.proxy",
    "httpcore.socks",
)
_TRACE_LOCK = threading.Lock()


class _PrivateTraceFilter(logging.Filter):
    def filter(self, record):
        if _PRIVATE_TRACE.get():
            # httpcore DEBUG includes raw response headers/exception repr, which
            # can contain Location signatures or Set-Cookie. Only our active
            # context is redacted; other projects/threads keep their logging.
            record.msg, record.args = (
                "Boyesir transport event (private details redacted)",
                (),
            )
            record.exc_info = record.exc_text = record.stack_info = None
        return True


_TRACE_FILTER = _PrivateTraceFilter()


@contextmanager
def _private_transport_trace():
    with _TRACE_LOCK:
        for name in _TRACE_LOGGERS:
            logger = logging.getLogger(name)
            if _TRACE_FILTER not in logger.filters:
                logger.addFilter(_TRACE_FILTER)
    token = _PRIVATE_TRACE.set(True)
    try:
        yield
    finally:
        _PRIVATE_TRACE.reset(token)


@dataclass(frozen=True)
class ModelContract:
    model_id: str
    resolution: str
    minimum_seconds: int
    maximum_seconds: int
    max_images: int
    max_videos: int
    max_audios: int
    evidence: str = "public_documentation_2026-10-04_not_inference_verified"


MODELS = {
    "bh-minimax-h3-pro-768p": ModelContract(
        "bh-minimax-h3-pro-768p", "768p", 4, 15, 9, 0, 3
    ),
    "bh-hailuo-h3-2k": ModelContract("bh-hailuo-h3-2k", "2k", 6, 10, 9, 3, 3),
}
BLOCKED_MODELS = {
    "lec-minimax-h3-768p": "historical_image_success_does_not_establish_current_complete_contract",
    "minimax-h3-768p": "distinct_model_reference_capabilities_unconfirmed",
    "lec-minimax-h3": "distinct_720p_model_reference_capabilities_unconfirmed",
}


def _require(value, code):
    if not value:
        raise BackendError(code)


def _identity(value):
    _require(isinstance(value, str) and ID.fullmatch(value), "boyesir_invalid_identity")
    return value


def _finite(value, low, high):
    return type(value) in (int, float) and math.isfinite(value) and low <= value <= high


def _digest(value):
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode()
    ).hexdigest()


def _safe_url(value, hosts):
    """Only trusted exact DNS hosts; no userinfo, ports, fragment or ambiguous URLs."""
    valid = isinstance(value, str) and 1 <= len(value) <= 8192
    if valid:
        valid = (
            not any(ord(c) <= 32 or ord(c) == 127 for c in value) and "\\" not in value
        )
    try:
        parts = urlsplit(value) if valid else None
        valid = bool(
            valid
            and parts.scheme == "https"
            and parts.hostname in hosts
            and parts.netloc == parts.hostname
            and not parts.username
            and not parts.password
            and not parts.fragment
            and parts.path.startswith("/")
            and not parts.path.startswith("//")
        )
    except (ValueError, TypeError):
        valid = False
    _require(valid, "boyesir_media_url_rejected")
    return value


@dataclass(frozen=True)
class ApprovedMedia:
    """Only a trusted resolver may mint these after ownership/provider approval.

    URL possession alone is not ownership proof. No URL or expiry is persisted in
    jobs; the resolver can renew it for the same asset SHA before submission.
    """

    tenant_id: str
    owner_id: str
    project_id: str
    asset_id: str
    kind: str
    model_id: str
    sha256: str
    expires_at: float
    url: str = field(repr=False)

    def __reduce_ex__(self, protocol):
        raise TypeError("Approved media URLs must remain process-local")


class MediaResolver(Protocol):
    def resolve(
        self,
        *,
        tenant_id: str,
        owner_id: str,
        project_id: str,
        asset_id: str,
        kind: str,
        model_id: str,
    ) -> ApprovedMedia: ...


@dataclass(frozen=True)
class SubmissionBinding:
    tag: str
    job_id: str
    tenant_id: str
    owner_id: str
    project_id: str
    model_id: str
    request_sha256: str


@dataclass(frozen=True)
class SubmissionRecord:
    binding: SubmissionBinding
    task_id: str | None


class SubmitGate(Protocol):
    """Trusted shared-ledger adapter. Fail closed; never use a process-local set.

    consume must atomically fence/check owner/job/attempt/current immutable request,
    reserve budget, and commit intent BEFORE returning True, exactly once across
    all processes. False/exception never authorizes POST. record_accepted commits
    the exact task ID to that attempt; lookup restores it after process restart.
    A WorkerRunner integration must delegate its existing begin_submission here
    exactly once (not call both). No implementation is installed by this module.
    """

    def consume(self, binding: SubmissionBinding) -> bool: ...
    def record_accepted(self, binding: SubmissionBinding, task_id: str) -> None: ...
    def lookup(self, tag: str) -> SubmissionRecord | None: ...


@dataclass(frozen=True)
class PreparedSubmission:
    binding: SubmissionBinding
    expires_at: float
    body: bytes = field(repr=False)
    seal: bytes = field(repr=False)

    def __reduce_ex__(self, protocol):
        raise TypeError("Prepared URLs and prompts must remain process-local")


def validate_request(request):
    """Deliberately separate schema: never strip normal H3 controls/defaults."""
    _require(
        isinstance(request, dict)
        and set(request) == {"recipe_id", "provider_request"}
        and request["recipe_id"] == "boyesir-video-v1",
        "boyesir_request_contract_required",
    )
    body = request["provider_request"]
    fields = {
        "model",
        "prompt",
        "duration",
        "resolution",
        "ratio",
        "output_audio",
        "media",
    }
    _require(
        isinstance(body, dict) and set(body) == fields,
        "boyesir_unsupported_or_missing_control",
    )
    model = (
        MODELS.get(body.get("model")) if isinstance(body.get("model"), str) else None
    )
    _require(model is not None, "boyesir_model_contract_blocked")
    _require(
        isinstance(body["prompt"], str)
        and bool(body["prompt"].strip())
        and len(body["prompt"].encode("utf-8")) <= 32000
        and "\x00" not in body["prompt"],
        "boyesir_prompt_invalid",
    )
    _require(
        type(body["duration"]) is int
        and model.minimum_seconds <= body["duration"] <= model.maximum_seconds,
        "boyesir_duration_outside_declared_contract",
    )
    _require(body["resolution"] == model.resolution, "boyesir_resolution_tier_mismatch")
    _require(
        body["ratio"] == "provider_default"
        and body["output_audio"] == "provider_default",
        "boyesir_ratio_or_output_audio_control_unconfirmed",
    )
    media = body["media"]
    _require(isinstance(media, list) and len(media) <= 15, "boyesir_reference_limit")
    counts = {"image": 0, "video": 0, "audio": 0}
    identities = set()
    for item in media:
        _require(
            isinstance(item, dict) and set(item) == {"asset_id", "kind", "sha256"},
            "boyesir_reference_contract_invalid",
        )
        _identity(item["asset_id"])
        _require(
            isinstance(item["kind"], str)
            and item["kind"] in counts
            and isinstance(item["sha256"], str)
            and SHA.fullmatch(item["sha256"]),
            "boyesir_reference_contract_invalid",
        )
        _require(item["asset_id"] not in identities, "boyesir_duplicate_reference")
        identities.add(item["asset_id"])
        counts[item["kind"]] += 1
    _require(
        counts["image"] <= model.max_images
        and counts["video"] <= model.max_videos
        and counts["audio"] <= model.max_audios,
        "boyesir_model_reference_limit",
    )
    _require(
        not counts["audio"] or counts["image"] or counts["video"],
        "boyesir_audio_requires_visual_reference",
    )
    # Freeze an independent JSON copy: callers cannot mutate accepted defaults.
    return json.loads(json.dumps(body, ensure_ascii=False, allow_nan=False))


def _binding(job, tag):
    _identity(tag)
    _require(isinstance(job, dict), "boyesir_job_contract_required")
    for key in ("id", "tenant_id", "owner_id", "project_id"):
        _identity(job.get(key))
    body = validate_request(job.get("request"))
    return SubmissionBinding(
        tag,
        job["id"],
        job["tenant_id"],
        job["owner_id"],
        job["project_id"],
        body["model"],
        _digest(body),
    ), body


def _loader(registry_root):
    _require(
        registry_root is not None and Path(registry_root).is_absolute(),
        "boyesir_central_path_required",
    )
    try:
        root = Path(registry_root).resolve(strict=True)
        expected = root / "api_registry.py"
        _require(expected.is_file(), "boyesir_central_loader_missing")
        old = sys.modules.get("api_registry")
        _require(
            old is None or Path(getattr(old, "__file__", "")).resolve() == expected,
            "boyesir_central_loader_conflict",
        )
        sys.path.insert(0, str(root))
        try:
            module = importlib.import_module("api_registry")
        finally:
            sys.path.remove(str(root))
        return module.load_api
    except Exception:
        raise NotReady("boyesir_central_loader_unavailable") from None


class BoyesirBackend:
    """Future API transport, deliberately unregistered in API/capabilities/worker."""

    kind = "boyesir-api"
    integration_ready = False

    @staticmethod
    def capabilities():
        return {
            "backend": "boyesir-api",
            "integration_ready": False,
            "online_verified": False,
            "models": {
                key: {
                    "resolution_tier": value.resolution,
                    "duration_seconds": [value.minimum_seconds, value.maximum_seconds],
                    "max_images": value.max_images,
                    "max_videos": value.max_videos,
                    "max_audios": value.max_audios,
                    "evidence": value.evidence,
                }
                for key, value in MODELS.items()
            },
            "blocked_models": dict(BLOCKED_MODELS),
            "ratio": ["provider_default"],
            "output_audio": ["provider_default"],
            "exact_pixel_dimensions": None,
            "cancel_supported": False,
            "provider_idempotency_verified": False,
            "max_concurrency": None,
            "billing_verified": False,
            "blocked_controls": [
                "first_frame",
                "last_frame",
                "seed",
                "steps",
                "sampler",
                "guides",
                "vae",
                "exact_dimensions",
            ],
        }

    def __init__(
        self,
        *,
        enabled=False,
        submit_gate=None,
        media_resolver=None,
        input_hosts=(),
        registry_root=None,
        credential_loader=None,
        transport=None,
        download_transport=None,
        max_download_bytes=512 * MIB,
        timeout_s=10,
        max_transfer_seconds=180,
        min_input_validity_s=900,
        clock=time.time,
        monotonic=time.monotonic,
        actual_cost_resolver=None,
    ):
        _require(type(enabled) is bool, "boyesir_enable_flag_invalid")
        _require(
            type(max_download_bytes) is int
            and 1 <= max_download_bytes <= 512 * MIB
            and _finite(timeout_s, 1, 30)
            and _finite(max_transfer_seconds, 1, 600)
            and _finite(min_input_validity_s, 60, 86400),
            "boyesir_transport_limits_invalid",
        )
        hosts = set(input_hosts)
        for host in hosts:
            _require(
                isinstance(host, str)
                and re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", host)
                and "." in host
                and ".." not in host
                and not host.endswith(".localhost"),
                "boyesir_input_host_invalid",
            )
            try:
                ipaddress.ip_address(host)
            except ValueError:
                pass
            else:
                raise BackendError("boyesir_input_host_invalid")
        self.enabled = enabled
        self.gate, self.resolver, self.input_hosts = (
            submit_gate,
            media_resolver,
            frozenset(hosts),
        )
        self.registry_root, self.credential_loader = registry_root, credential_loader
        self._transport, self._downloads = transport, download_transport
        self.max_bytes, self.timeout_s, self.transfer_s = (
            max_download_bytes,
            timeout_s,
            max_transfer_seconds,
        )
        self.min_validity, self.clock, self.monotonic = (
            min_input_validity_s,
            clock,
            monotonic,
        )
        self.cost_resolver = actual_cost_resolver
        self.slot_key = "boyesir-api-" + PROFILE
        self._key = None
        self._prepare_seal_key = os.urandom(32)

    def __repr__(self):
        return f"BoyesirBackend(enabled={self.enabled}, integration_ready=False)"

    def __reduce_ex__(self, protocol):
        raise TypeError("Provider credentials must remain process-local")

    def _ready(self):
        if not self.enabled:
            raise NotReady("backend_disabled")
        if self.gate is None:
            raise NotReady("boyesir_shared_submit_gate_required")

    def _seal(self, binding, expiry, body):
        # Not a provider credential: process-only integrity for a trusted prepare.
        # A caller cannot replace body/binding/expiry with a matching plain hash.
        material = json.dumps(
            {"binding": vars(binding), "expires_at": expiry},
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
        return hmac.digest(self._prepare_seal_key, material + b"\x00" + body, "sha256")

    def _credential(self):
        self._ready()
        if self._key is None:
            try:
                load = self.credential_loader or _loader(self.registry_root)
                config = load(SERVICE, profile=PROFILE)
                _require(
                    config.service == SERVICE
                    and config.profile == PROFILE
                    and config.base_url == BASE_URL,
                    "boyesir_central_profile_mismatch",
                )
                key = config.api_key
                _require(
                    isinstance(key, str)
                    and key.isascii()
                    and 1 <= len(key) <= 4096
                    and not any(
                        c.isspace() or ord(c) < 32 or ord(c) == 127 for c in key
                    ),
                    "boyesir_credential_missing",
                )
                self._key = key
            except Exception:
                raise NotReady("boyesir_central_profile_unavailable") from None
        return self._key

    def _transport_for(self, *, download=False):
        name = "_downloads" if download else "_transport"
        current = getattr(self, name)
        if current is None:
            current = httpx.HTTPTransport(retries=0, trust_env=False)
            setattr(self, name, current)
        return current

    def close(self):
        with _private_transport_trace():
            for transport in {self._transport, self._downloads} - {None}:
                transport.close()
        self._transport = self._downloads = None
        self._key = None

    def _request(self, method, url, *, body=None, download=False):
        headers = {"Accept-Encoding": "identity"}
        if not download:
            _require(
                url == BASE_URL + "/v1/videos/generations"
                or url.startswith(BASE_URL + "/v1/tasks/"),
                "boyesir_api_endpoint_rejected",
            )
            headers.update(
                {
                    "Authorization": "Bearer " + self._credential(),
                    "Accept": "application/json",
                }
            )
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = httpx.Request(
            method,
            url,
            headers=headers,
            content=body,
            extensions={
                "timeout": {
                    k: self.timeout_s for k in ("connect", "read", "write", "pool")
                }
            },
        )
        # Direct transport has no automatic redirects, cookies, retries, default
        # auth or Client INFO logging of bearer/signed URLs. Do not print request.
        return self._transport_for(download=download).handle_request(request)

    def _json(self, method, path, *, body=None):
        self._ready()
        try:
            started = self.monotonic()
            with (
                _private_transport_trace(),
                closing(self._request(method, BASE_URL + path, body=body)) as response,
            ):
                status = response.status_code
                if not 200 <= status < 300:
                    return status, None
                _require(
                    response.headers.get("content-encoding", "identity").lower()
                    in {"", "identity"},
                    "boyesir_response_encoding_rejected",
                )
                content = bytearray()
                for chunk in response.iter_bytes():
                    content.extend(chunk)
                    _require(
                        len(content) <= MIB
                        and self.monotonic() - started <= self.transfer_s,
                        "boyesir_response_limit",
                    )
                data = json.loads(content)
                _require(isinstance(data, dict), "boyesir_response_invalid")
                return status, data
        except Exception:
            raise BackendError("boyesir_response_unavailable") from None

    def prepare(self, job, tag, store, heartbeat):
        self._ready()
        binding, body = _binding(job, tag)
        self._credential()  # Fail before consuming the nonrepeatable submission intent.
        heartbeat()
        payload = {
            key: body[key] for key in ("model", "prompt", "duration", "resolution")
        }
        expiry = self.clock() + 86400
        for item in body["media"]:
            if self.resolver is None:
                raise NotReady("boyesir_trusted_media_resolver_required")
            try:
                media = self.resolver.resolve(
                    tenant_id=binding.tenant_id,
                    owner_id=binding.owner_id,
                    project_id=binding.project_id,
                    asset_id=item["asset_id"],
                    kind=item["kind"],
                    model_id=binding.model_id,
                )
                _require(
                    isinstance(media, ApprovedMedia)
                    and (
                        media.tenant_id,
                        media.owner_id,
                        media.project_id,
                        media.asset_id,
                        media.kind,
                        media.model_id,
                        media.sha256,
                    )
                    == (
                        binding.tenant_id,
                        binding.owner_id,
                        binding.project_id,
                        item["asset_id"],
                        item["kind"],
                        binding.model_id,
                        item["sha256"],
                    ),
                    "boyesir_media_approval_mismatch",
                )
                _require(
                    _finite(
                        media.expires_at,
                        self.clock() + self.min_validity,
                        self.clock() + 7 * 86400,
                    ),
                    "boyesir_media_approval_expired",
                )
                url = _safe_url(media.url, self.input_hosts)
                expiry = min(expiry, media.expires_at)
                payload.setdefault(
                    {"image": "images", "video": "videos", "audio": "audios"}[
                        item["kind"]
                    ],
                    [],
                ).append(url)
            except Exception:
                raise BackendError("boyesir_media_not_approved") from None
            heartbeat()
        encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode()
        return PreparedSubmission(
            binding, expiry, encoded, self._seal(binding, expiry, encoded)
        )

    def submit(self, prepared, tag):
        self._ready()
        _require(
            isinstance(prepared, PreparedSubmission) and prepared.binding.tag == tag,
            "boyesir_prepared_identity_mismatch",
        )
        try:
            _require(
                isinstance(prepared.seal, bytes)
                and isinstance(prepared.body, bytes)
                and hmac.compare_digest(
                    prepared.seal,
                    self._seal(prepared.binding, prepared.expires_at, prepared.body),
                ),
                "boyesir_prepared_integrity_invalid",
            )
        except Exception:
            raise BackendError("boyesir_prepared_integrity_invalid") from None
        _require(
            prepared.expires_at >= self.clock() + self.min_validity,
            "boyesir_prepared_media_expired",
        )
        self._credential()
        try:
            consumed = self.gate.consume(prepared.binding)
        except Exception:
            raise SubmissionUncertain("boyesir_submit_gate_unknown") from None
        if consumed is not True:
            raise SubmissionUncertain("boyesir_submission_already_intended")
        try:
            status, data = self._json(
                "POST", "/v1/videos/generations", body=prepared.body
            )
            if status == 402:
                # Official contract: insufficient balance was not charged. No
                # other status is treated as proof that submission did not occur.
                raise SubmissionRejected("boyesir_insufficient_balance")
            _require(
                200 <= status < 300 and isinstance(data, dict),
                "boyesir_create_response_invalid",
            )
            task_id = _identity(data.get("task_id"))
            self.gate.record_accepted(prepared.binding, task_id)
            return task_id
        except SubmissionRejected:
            raise
        except Exception:
            raise SubmissionUncertain("boyesir_submission_response_unknown") from None

    def _record(self, tag, task_id=None):
        self._ready()
        _identity(tag)
        if task_id is not None:
            _identity(task_id)
        try:
            record = self.gate.lookup(tag)
            _require(
                record is None or isinstance(record, SubmissionRecord),
                "boyesir_ledger_record_invalid",
            )
            if record is None:
                _require(task_id is None, "boyesir_task_not_bound")
                return None
            _require(record.binding.tag == tag, "boyesir_task_not_bound")
            if record.task_id is not None:
                _identity(record.task_id)
            _require(
                task_id is None or record.task_id == task_id, "boyesir_task_not_bound"
            )
            return record
        except Exception:
            raise BackendError("boyesir_task_not_bound") from None

    def reconcile(self, tag, task_id=None):
        record = self._record(tag, task_id)
        if record is None or record.task_id is None:
            return Outcome("unknown")
        return self.poll(tag, record.task_id)

    def _task(self, tag, task_id):
        self._record(tag, task_id)
        status, data = self._json("GET", "/v1/tasks/" + task_id)
        if not 200 <= status < 300:
            raise BackendError("boyesir_task_query_unavailable")
        _require(
            "task_id" not in data or data["task_id"] == task_id,
            "boyesir_task_echo_mismatch",
        )
        return data

    def poll(self, tag, task_id):
        data = self._task(tag, task_id)
        raw_state = data.get("status")
        state = (
            {
                "queued": "running",
                "processing": "running",
                "succeeded": "succeeded",
                "failed": "failed",
            }.get(raw_state, "unknown")
            if isinstance(raw_state, str)
            else "unknown"
        )
        # Upstream error text/result URLs and refund claims never enter Outcome.
        return Outcome(state, task_id)

    def cancel(self, tag, task_id):
        self._record(tag, task_id)
        return False  # No public stop/cancel contract; never imply billing stopped.

    @staticmethod
    def _target(directory):
        target = Path(directory)
        _require(
            target.is_absolute() and ".." not in target.parts,
            "boyesir_output_directory_invalid",
        )
        for path in (target, *target.parents):
            _require(
                path.exists()
                and not path.is_symlink()
                and not path.is_junction()
                and path.is_dir(),
                "boyesir_output_directory_invalid",
            )
        output = target / "boyesir-raw.mp4"
        if output.exists() or output.is_symlink():
            info = output.lstat()
            _require(
                stat.S_ISREG(info.st_mode)
                and info.st_nlink == 1
                and not output.is_symlink(),
                "boyesir_output_file_invalid",
            )
        return output

    def fetch(self, job, tag, task_id, target_dir, heartbeat):
        binding, _ = _binding(job, tag)
        record = self._record(tag, task_id)
        _require(
            record is not None and record.binding == binding,
            "boyesir_output_owner_or_request_mismatch",
        )
        data = self._task(tag, task_id)
        result = data.get("result")
        videos = result.get("videos") if isinstance(result, dict) else None
        _require(
            data.get("status") == "succeeded"
            and isinstance(videos, list)
            and len(videos) == 1,
            "boyesir_single_output_contract_required",
        )
        url = _safe_url(videos[0], RESULT_HOSTS)
        output = self._target(target_dir)
        partial = output.with_name(".boyesir-" + uuid.uuid4().hex + ".part")
        count, started = 0, self.monotonic()
        heartbeat()
        try:
            with (
                _private_transport_trace(),
                closing(self._request("GET", url, download=True)) as response,
            ):
                _require(response.status_code == 200, "boyesir_download_http_rejected")
                _require(
                    response.headers.get("content-encoding", "identity").lower()
                    in {"", "identity"},
                    "boyesir_download_encoding_rejected",
                )
                length = response.headers.get("content-length")
                if length is not None:
                    _require(
                        re.fullmatch(r"[0-9]{1,12}", length)
                        and 0 < int(length) <= self.max_bytes,
                        "boyesir_download_size_rejected",
                    )
                with partial.open("xb") as destination:
                    os.chmod(partial, 0o600)
                    for chunk in response.iter_bytes():
                        heartbeat()
                        count += len(chunk)
                        _require(
                            count <= self.max_bytes
                            and self.monotonic() - started <= self.transfer_s,
                            "boyesir_download_limit",
                        )
                        destination.write(chunk)
                    _require(
                        count > 0 and (length is None or count == int(length)),
                        "boyesir_download_incomplete",
                    )
                    destination.flush()
                    os.fsync(destination.fileno())
            self._target(target_dir)
            partial.replace(output)
            return {
                "video": output
            }  # Bytes only; collector MUST decode/probe before publication.
        except Exception:
            raise BackendError("boyesir_download_unavailable") from None
        finally:
            # Only our random, exclusively-created temporary file; no recursive cleanup.
            try:
                if partial.exists() and not partial.is_symlink():
                    partial.unlink()
            except OSError:
                raise BackendError("boyesir_partial_cleanup_failed") from None
