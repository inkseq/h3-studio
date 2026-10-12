"""Private WanGP protocol. Importing it has no runtime or network side effects.

JSON is stored canonically as strings in frozen values so a caller cannot mutate
accepted settings through a retained dict. These are execution receipts, not jobs.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import hashlib
import json
import math
from pathlib import Path
import re
from typing import Any, Iterable, Mapping, Protocol

from .protocol import TAG

UPSTREAM_REVISION = "0e58385fbde7ff102d276e4a9e490845de76b4ea"
PROTOCOL_VERSION = "wangp-private-v1"
_SHA = re.compile(r"^[0-9a-f]{64}$")
_ID = re.compile(r"^[A-Za-z0-9_.-]{1,200}$")
TERMINAL = frozenset({"succeeded", "failed", "cancelled"})
STATES = TERMINAL | {"prepared", "dispatch_intent", "running", "sealing", "unknown"}


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _object(text: str) -> dict:
    if not isinstance(text, str) or len(text.encode("utf-8")) > 1024 * 1024:
        raise ValueError("wangp_invalid_json")
    value = json.loads(text)
    if not isinstance(value, dict) or canonical_json(value) != text:
        raise ValueError("wangp_noncanonical_object")
    return value


def _identifier(value: str) -> None:
    if not isinstance(value, str) or not _ID.fullmatch(value) or value in {".", ".."}:
        raise ValueError("wangp_invalid_identity")


def _sha(value: str) -> None:
    if not isinstance(value, str) or not _SHA.fullmatch(value):
        raise ValueError("wangp_invalid_digest")


def operation_id(attempt_tag: str) -> str:
    if not isinstance(attempt_tag, str) or not TAG.fullmatch(attempt_tag):
        raise ValueError("wangp_invalid_attempt_tag")
    return "wangp-" + attempt_tag


@dataclass(frozen=True)
class EngineManifest:
    document_json: str

    def __post_init__(self):
        data = _object(self.document_json)
        if (data.get("engine") != "wangp" or data.get("source_revision") != UPSTREAM_REVISION
                or data.get("protocol_version") != PROTOCOL_VERSION):
            raise ValueError("wangp_manifest_revision_mismatch")
        for key in ("compiler_id", "profile_id", "runtime_digest", "memory_profile", "kernel_profile"):
            if not isinstance(data.get(key), str) or not data[key].strip():
                raise ValueError("wangp_incomplete_manifest")
        if not isinstance(data.get("components"), dict) or not data["components"]:
            raise ValueError("wangp_components_required")
        for component in data["components"].values():
            if not isinstance(component, dict) or any(not isinstance(component.get(k), str)
                    or not component[k] or component[k] in {"main", "latest"}
                    for k in ("revision", "precision")):
                raise ValueError("wangp_unpinned_component")
        topology = data.get("topology")
        if not isinstance(topology, dict) or topology.get("slots") != 1:
            raise ValueError("wangp_one_slot_required")
        if "synthetic" in data and type(data["synthetic"]) is not bool:
            raise ValueError("wangp_invalid_synthetic_marker")

    @classmethod
    def from_dict(cls, document: Mapping[str, Any]) -> "EngineManifest":
        return cls(canonical_json(document))

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.document_json.encode("utf-8")).hexdigest()

    @property
    def document(self) -> dict:
        return json.loads(self.document_json)


@dataclass(frozen=True)
class InputDescriptor:
    asset_id: str
    handle: str
    kind: str
    sha256: str
    size_bytes: int

    def __post_init__(self):
        _identifier(self.asset_id)
        _identifier(self.handle)
        _sha(self.sha256)
        if self.kind not in {"image", "video", "audio"} or type(self.size_bytes) is not int or self.size_bytes <= 0:
            raise ValueError("wangp_invalid_input")


@dataclass(frozen=True)
class PreparedRequest:
    job_id: str
    attempt_tag: str
    request_hash: str
    manifest_digest: str
    settings_json: str = field(repr=False)
    output_spec_json: str
    generate_audio: bool
    inputs: tuple[InputDescriptor, ...] = ()

    def __post_init__(self):
        _identifier(self.job_id)
        operation_id(self.attempt_tag)
        _sha(self.request_hash)
        _sha(self.manifest_digest)
        _object(self.settings_json)
        if not _object(self.output_spec_json) or type(self.generate_audio) is not bool:
            raise ValueError("wangp_output_spec_required")
        if not isinstance(self.inputs, tuple) or any(not isinstance(v, InputDescriptor) for v in self.inputs):
            raise ValueError("wangp_immutable_inputs_required")
        if len({v.asset_id for v in self.inputs}) != len(self.inputs):
            raise ValueError("wangp_duplicate_input")

    @property
    def operation_id(self) -> str:
        return operation_id(self.attempt_tag)

    @property
    def settings(self) -> dict:
        return json.loads(self.settings_json)

    @property
    def identity_digest(self) -> str:
        return digest(asdict(self))


@dataclass(frozen=True)
class ArtifactDescriptor:
    kind: str
    sha256: str
    size_bytes: int
    media_type: str

    def __post_init__(self):
        _sha(self.sha256)
        allowed = {"video": {"video/mp4"}, "audio": {"audio/flac", "audio/wav"}}
        if (self.kind not in allowed or self.media_type not in allowed[self.kind]
                or type(self.size_bytes) is not int or self.size_bytes <= 0):
            raise ValueError("wangp_invalid_artifact")


@dataclass(frozen=True)
class OperationReceipt:
    operation_id: str
    job_id: str
    attempt_tag: str
    request_hash: str
    manifest_digest: str
    identity_digest: str
    slot_key: str
    incarnation: str
    state: str
    generate_audio: bool
    stop_proven: bool = False
    cancel_requested: bool = False
    reason: str = ""
    hold_reason: str = ""
    artifacts: tuple[ArtifactDescriptor, ...] = ()

    def __post_init__(self):
        if self.operation_id != operation_id(self.attempt_tag) or self.state not in STATES:
            raise ValueError("wangp_invalid_receipt")
        for value in (self.job_id, self.slot_key, self.incarnation):
            _identifier(value)
        for value in (self.request_hash, self.manifest_digest, self.identity_digest):
            _sha(value)
        if any(type(v) is not bool for v in (self.generate_audio, self.stop_proven, self.cancel_requested)):
            raise ValueError("wangp_invalid_receipt_flags")
        for value in (self.reason, self.hold_reason):
            if value:
                _identifier(value)
        if self.state in TERMINAL and self.stop_proven is not True:
            raise ValueError("wangp_terminal_requires_stop_proof")
        if not isinstance(self.artifacts, tuple) or any(not isinstance(v, ArtifactDescriptor) for v in self.artifacts):
            raise ValueError("wangp_invalid_receipt_artifacts")
        kinds = {v.kind for v in self.artifacts}
        if len(kinds) != len(self.artifacts):
            raise ValueError("wangp_duplicate_artifact")
        expected = {"video", "audio"} if self.generate_audio else {"video"}
        if self.state == "succeeded" and kinds != expected:
            raise ValueError("wangp_incomplete_outputs")

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "OperationReceipt":
        data = dict(value)
        data["artifacts"] = tuple(ArtifactDescriptor(**v) for v in data.get("artifacts", ()))
        return cls(**data)


@dataclass(frozen=True)
class HostReadiness:
    manifest_digest: str
    slot_key: str
    incarnation: str
    idle: bool
    reason: str = ""
    preparation: dict | None = field(default=None, repr=False)


def safe_model_progress(value):
    """An optional native observation never changes execution eligibility."""
    if (not isinstance(value, dict) or type(value.get("state")) is not str or value["state"] not in
            {"not_loaded", "loading", "loaded", "not_observed"}):
        return None
    result = {"state": value["state"]}
    for key in ("updated_at", "phase_started_at"):
        item = value.get(key)
        result[key] = item if type(item) in (int, float) and math.isfinite(item) and 0 <= item <= 4_102_444_800 else None
    if type(value.get("reason_code")) is str and value["reason_code"] in {"wangp_model_load_failed", "wangp_model_load_unobserved"}:
        result["reason_code"] = value["reason_code"]
    return result


@dataclass(frozen=True)
class RuntimeOutput:
    """Trusted facade output, private to host; never sent over status transport."""
    path: Path
    media_type: str


@dataclass(frozen=True)
class RuntimeObservation:
    state: str
    stopped: bool = False
    outputs: Mapping[str, RuntimeOutput] = field(default_factory=dict, repr=False)
    error_code: str | None = None


class RuntimeHandle(Protocol):
    def observe(self) -> RuntimeObservation: ...
    def cancel(self) -> bool: ...


class RuntimeSession(Protocol):
    """D2 wraps the upstream SessionJob; this is not its native Python API."""
    def submit_task(self, settings: Mapping[str, Any]) -> RuntimeHandle: ...
    def is_idle(self) -> bool: ...


class WanGPTransport(Protocol):
    def submit(self, prepared: PreparedRequest) -> OperationReceipt: ...
    def inspect(self, operation_id: str) -> OperationReceipt | None: ...
    def cancel(self, operation_id: str) -> bool: ...
    def read_artifact(self, operation_id: str, kind: str) -> Iterable[bytes]: ...
    def readiness(self) -> HostReadiness: ...
