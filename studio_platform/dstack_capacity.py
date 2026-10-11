"""Pinned dstack capacity transport; business intents and obligations stay external.

Importing this module never connects to dstack or a provider. A caller injects
the existing transaction-backed ledger, not a second rental database. Each
initial run has one physical GPU and a persistent private WanGP Session.
"""
from __future__ import annotations

from dataclasses import dataclass
import argparse
import copy
import hashlib
import importlib
import json
import math
from pathlib import Path, PurePosixPath
import re
import time
from typing import Protocol
from urllib.parse import urlsplit
from uuid import UUID

import httpx

from .inference.wangp_contract import HostReadiness

DSTACK_VERSION = "0.22.3"
BACKENDS = frozenset({"vastai", "runpod"})
OWNER = "sixnine"
SHA256 = re.compile(r"[0-9a-f]{64}")
IDENTIFIER = re.compile(r"[A-Za-z0-9_.-]{1,200}")
TERMINAL = frozenset({"done", "failed", "aborted", "terminated"})
RUN_STATUSES = TERMINAL | frozenset({"pending", "submitted", "provisioning", "running", "terminating", "stopping"})


class DstackError(ValueError):
    """Static diagnostics only; do not wrap provider response/exception text."""


def _require(condition, code):
    if not condition:
        raise DstackError(code)


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
        allow_nan=False).encode()).hexdigest()


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
    hard_deadline: float
    max_duration_s: int
    cpu_count: int = 4
    gpu_memory_gib: int = 30
    gpu_count: int = 1
    min_reliability: float = 0.90
    regions: tuple[str, ...] = ()

    def __post_init__(self):
        _uuid(self.intent_id)
        _require(self.backend in BACKENDS, "dstack_backend_unsupported")
        _require(self.mode in {"fl", "ref"}, "dstack_mode_invalid")
        for identity in (self.profile_id, self.model_id, self.configuration_id):
            _require(isinstance(identity, str) and IDENTIFIER.fullmatch(identity), "dstack_identity_invalid")
        _require(isinstance(self.manifest_digest, str) and SHA256.fullmatch(self.manifest_digest),
            "dstack_manifest_invalid")
        _require(isinstance(self.image, str) and re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9./:_-]*@sha256:[0-9a-f]{64}", self.image), "dstack_image_digest_required")
        _require(type(self.gpu_count) is int and self.gpu_count == 1, "dstack_single_gpu_required")
        for count in (self.memory_gib, self.disk_gib, self.max_price_microusd,
                      self.max_duration_s, self.cpu_count, self.gpu_memory_gib):
            _require(type(count) is int and count > 0, "dstack_resource_limit_invalid")
        _require(self.max_duration_s <= 86400, "dstack_duration_invalid")
        _require(isinstance(self.gpu_names, tuple) and 1 <= len(self.gpu_names) <= 8
            and all(isinstance(n, str) and re.fullmatch(r"[A-Za-z0-9 _.-]{1,100}", n) for n in self.gpu_names),
            "dstack_gpu_selection_invalid")
        _require(isinstance(self.regions, tuple) and len(self.regions) <= 20 and
            all(isinstance(n, str) and IDENTIFIER.fullmatch(n) for n in self.regions), "dstack_region_invalid")
        _require(all(type(t) in (int, float) and math.isfinite(t) and t > 0
            for t in (self.created_at, self.hard_deadline)) and self.created_at < self.hard_deadline,
            "dstack_deadline_invalid")
        _require(type(self.min_reliability) in (int, float) and math.isfinite(self.min_reliability)
            and 0.9 <= self.min_reliability <= 1, "dstack_reliability_invalid")

    @property
    def run_name(self):
        return "sixnine-" + self.intent_id.replace("-", "")

    def run_spec(self, ssh_key_pub):
        _require(isinstance(ssh_key_pub, str) and re.fullmatch(
            r"ssh-(?:ed25519|rsa) [A-Za-z0-9+/=]+(?: [^\r\n]*)?", ssh_key_pub), "dstack_public_ssh_key_invalid")
        # The image contains prepared dependencies. CPU uploads the existing
        # hash-bound bundle and starts the loopback launcher through SSH.
        configuration = {"type": "task", "name": self.run_name, "image": self.image,
            "commands": ["exec sleep infinity"], "backends": [self.backend],
            "resources": {"cpu": {"arch": "x86", "count": f"{self.cpu_count}.."},
                "memory": f"{self.memory_gib}GB..", "disk": {"size": f"{self.disk_gib}GB.."},
                "gpu": {"vendor": "nvidia", "name": list(self.gpu_names), "count": 1,
                    "memory": f"{self.gpu_memory_gib}GB.."}},
            "max_price": self.max_price_microusd / 1_000_000, "max_duration": self.max_duration_s,
            "retry": False, "spot_policy": "on-demand", "dstack": False,
            "env": {}, "ports": [], "stop_duration": 300,
            "regions": list(self.regions) if self.regions else None, "tags": {"sixnine_owner": OWNER,
                "sixnine_intent": self.intent_id, "sixnine_profile": self.profile_id,
                "sixnine_mode": self.mode, "sixnine_manifest": self.manifest_digest}}
        if self.backend == "vastai":
            configuration["backend_options"] = [{"type": "vastai", "offer_order": "price",
                "min_reliability": self.min_reliability}]
        spec = {"run_name": self.run_name, "configuration": configuration, "ssh_key_pub": ssh_key_pub}
        configuration["tags"]["sixnine_spec"] = _hash(spec)
        return spec

    def binding(self, ssh_key_pub):
        spec = self.run_spec(ssh_key_pub)
        return {"intent_id": self.intent_id, "run_name": self.run_name, "backend": self.backend,
            "profile_id": self.profile_id, "mode": self.mode, "model_id": self.model_id,
            "configuration_id": self.configuration_id, "manifest_digest": self.manifest_digest,
            "created_at": self.created_at, "hard_deadline": self.hard_deadline,
            "spec_digest": _hash(spec), "run_spec": spec}


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
    def begin_stop(self, intent_id: str, run_id: str, *, retry: bool = False) -> bool: ...


class DstackClient:
    """Exact 0.22.3 REST requests, no automatic side-effect retry."""
    def __init__(self, endpoint, token, project, *, session=None, timeout=30):
        parsed = urlsplit(endpoint)
        _require(parsed.scheme == "https" or parsed.scheme == "http" and
            parsed.hostname in {"127.0.0.1", "localhost", "::1"}, "dstack_endpoint_invalid")
        _require(not parsed.username and not parsed.password and not parsed.query and not parsed.fragment
            and parsed.path in {"", "/"}, "dstack_endpoint_invalid")
        _require(isinstance(project, str) and re.fullmatch(r"[a-z][a-z0-9-]{0,62}", project),
            "dstack_project_invalid")
        _require(isinstance(token, str) and 16 <= len(token) <= 4096 and not any(c.isspace() for c in token),
            "dstack_token_invalid")
        _require(type(timeout) in (int, float) and 0 < timeout <= 120, "dstack_timeout_invalid")
        self.endpoint, self.project, self.timeout = endpoint.rstrip("/"), project, timeout
        self._headers = {"Authorization": "Bearer " + token}
        self._session = session or httpx.Client(follow_redirects=False, trust_env=False)
        self._owns_session = session is None

    def close(self):
        if self._owns_session:
            self._session.close()

    def _post(self, path, body, *, absent=False):
        try:
            response = self._session.post(self.endpoint + path, json=body,
                headers=self._headers, timeout=self.timeout)
            if absent and response.status_code == 404:
                return None
            _require(len(response.content) <= 8 * 1024**2, "dstack_response_limit")
            # Pinned 0.22.3 returns its structured NotExistsError as HTTP 400.
            # Only an exact optional read may interpret that sole error as
            # absence; never treat other errors or a rejected mutation so.
            if absent and response.status_code == 400:
                document = response.json()
                detail = document.get("detail") if isinstance(document, dict) else None
                if (isinstance(detail, list) and len(detail) == 1
                        and isinstance(detail[0], dict)
                        and detail[0].get("code") == "resource_not_exists"):
                    return None
            _require(response.status_code < 300 and response.status_code >= 200, "dstack_api_rejected")
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
            _require(isinstance(run_name, str) and re.fullmatch(r"sixnine-[0-9a-f]{32}", run_name),
                "dstack_run_name_invalid")
        return self._post(f"/api/project/{self.project}/runs/get",
            {"id": run_id} if run_id else {"run_name": run_name}, absent=True)

    def plan(self, spec):
        # 0.22.3 ordinary planning selects an existing fleet. This endpoint is
        # our advisory global stock query; it does not create a fleet or run.
        return self._post(f"/api/project/{self.project}/runs/get_plan",
            {"run_spec": spec, "max_offers": 100, "for_offers_only": True})

    def apply(self, spec):
        # Null expected resource + force:false is dstack's create-only CAS.
        return self._post(f"/api/project/{self.project}/runs/apply",
            {"plan": {"run_spec": spec, "current_resource": None}, "force": False})

    def stop(self, run_name):
        _require(re.fullmatch(r"sixnine-[0-9a-f]{32}", run_name), "dstack_run_name_invalid")
        return self._post(f"/api/project/{self.project}/runs/stop", {"runs_names": [run_name], "abort": False})


def _owns(run, binding, project):
    """Compare the owned configuration subset, tolerating added server defaults."""
    _require(isinstance(run, dict) and run.get("project_name") == project, "dstack_run_ownership_mismatch")
    _uuid(run.get("id"))
    spec = run.get("run_spec", {})
    wanted = binding["run_spec"]
    _require(isinstance(spec, dict), "dstack_run_binding_mismatch")
    _require(spec.get("run_name") == binding["run_name"], "dstack_run_ownership_mismatch")
    actual = spec.get("configuration", {})
    expected = wanted["configuration"]
    _require(isinstance(actual, dict), "dstack_run_binding_mismatch")
    for key in ("type", "name", "image", "commands", "backends", "retry", "max_duration", "max_price", "tags"):
        _require(actual.get(key) == expected[key], "dstack_run_binding_mismatch")
    _require(actual.get("env", {}) in ({}, None) and actual.get("ports", []) in ([], None)
        and actual.get("dstack", False) is False, "dstack_run_binding_mismatch")
    _require(spec.get("ssh_key_pub") == wanted["ssh_key_pub"], "dstack_run_binding_mismatch")
    for key in ("privileged", "docker", "registry_auth", "entrypoint", "schedule", "groups", "nodes"):
        _require(actual.get(key) in (None, False), "dstack_run_binding_mismatch")
    for key in ("files", "repos", "setup", "volumes"):
        _require(actual.get(key) in (None, []), "dstack_run_binding_mismatch")
    _require(actual.get("spot_policy") == expected["spot_policy"]
        and actual.get("regions") == expected["regions"]
        and actual.get("stop_duration") == expected["stop_duration"], "dstack_run_binding_mismatch")
    _require(_resources(actual.get("resources")) == _resources(expected["resources"]),
        "dstack_run_binding_mismatch")
    wanted_options = expected.get("backend_options", [])
    actual_options = actual.get("backend_options") or []
    _require(isinstance(actual_options, list) and len(wanted_options) == len(actual_options) and all(
        isinstance(observed, dict) and
        all(observed.get(key) == value for key, value in desired.items())
        for observed, desired in zip(actual_options, wanted_options)), "dstack_run_binding_mismatch")
    if binding.get("run_id"):
        _require(run["id"] == binding["run_id"], "dstack_run_identity_changed")


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
    _require(all(isinstance(item, dict) for item in (cpu, gpu, disk)), "dstack_resource_response_invalid")
    return {"cpu_arch": cpu.get("arch"), "cpu_count": _range(cpu.get("count")),
        "memory": _range(value.get("memory")), "disk": _range(disk.get("size")),
        "gpu_vendor": gpu.get("vendor"), "gpu_name": gpu.get("name"),
        "gpu_count": _range(gpu.get("count")), "gpu_memory": _range(gpu.get("memory"))}


class DstackCapacity:
    def __init__(self, client: DstackClient, store: DstackStore, *, clock=time.time, readiness=None):
        self.client, self.store, self.clock, self.readiness = client, store, clock, readiness

    def plan(self, request, ssh_key_pub):
        _require(self.clock() < request.hard_deadline, "dstack_window_expired")
        value = self.client.plan(request.run_spec(ssh_key_pub))
        _require(isinstance(value, dict), "dstack_plan_invalid")
        # Offers are advisory, not allocation guarantees or permission to rent.
        return value

    def start(self, request, ssh_key_pub):
        _require(self.clock() < request.hard_deadline, "dstack_window_expired")
        binding = request.binding(ssh_key_pub)
        prior = self.store.load(request.intent_id)
        _require(isinstance(prior, dict), "dstack_intent_missing")
        if prior.get("spec_digest"):
            _require(prior["spec_digest"] == binding["spec_digest"], "dstack_intent_binding_changed")
        if prior.get("apply_started"):
            return self.observe(request.intent_id)
        # Exact pre-read refuses a collision; list absence is never proof.
        existing = self.client.get(run_name=request.run_name)
        _require(existing is None, "dstack_existing_run_requires_reconciliation")
        if not self.store.begin_apply(request.intent_id, binding):
            return self.observe(request.intent_id)
        try:
            run = self.client.apply(binding["run_spec"])
            _owns(run, binding, self.client.project)
            self.store.record(request.intent_id, {"run_id": run["id"], "state": "starting",
                "observed_at": self.clock(), "reason_code": "dstack_runtime_not_observed"})
        except DstackError:
            self.store.record(request.intent_id, {"state": "creation_unknown", "observed_at": self.clock(),
                "reason_code": "dstack_apply_requires_reconciliation"})
        return self.observe(request.intent_id)

    def observe(self, intent_id):
        binding = self.store.load(intent_id)
        _require(isinstance(binding, dict) and binding.get("apply_started") is True, "dstack_intent_not_submitted")
        try:
            run = self.client.get(run_id=binding["run_id"]) if binding.get("run_id") else self.client.get(run_name=binding["run_name"])
            if run is None:
                value = {"state": "creation_unknown" if not binding.get("stop_started") else "removal_unknown", "ready": False,
                    "reason_code": "dstack_exact_run_absent", "observed_at": self.clock()}
            else:
                _owns(run, binding, self.client.project)
                value = self._observation(run, binding)
        except DstackError as error:
            value = {"state": "observation_unknown", "ready": False, "reason_code": str(error), "observed_at": self.clock()}
        except Exception:
            value = {"state": "observation_unknown", "ready": False, "reason_code": "dstack_response_invalid", "observed_at": self.clock()}
        self.store.record(intent_id, value)
        return value

    def _observation(self, run, binding):
        status = run.get("status")
        _require(isinstance(status, str) and status in RUN_STATUSES, "dstack_run_status_invalid")
        value = {"run_id": run["id"], "run_name": binding["run_name"], "dstack_status": status,
            "state": "starting", "ready": False, "model_load_state": "not_observed",
            "billing_state": "unsettled", "observed_at": self.clock(),
            "reason_code": "dstack_runtime_not_observed"}
        # The library's modeled cost is an estimate, not a supplier invoice.
        cost = run.get("cost")
        if type(cost) in (int, float) and math.isfinite(cost) and cost >= 0:
            value["estimated_cost_microusd"] = round(cost * 1_000_000)
        if status in TERMINAL:
            value.update(state="stopped", reason_code="dstack_run_terminal_billing_pending")
            return value
        if status in {"terminating", "stopping"}:
            value.update(state="stopping", reason_code="dstack_stop_pending")
            return value
        if status != "running":
            if binding.get("stop_started"):
                value.update(state="stopping", reason_code="dstack_stop_pending")
            return value
        submission = run.get("latest_job_submission") or {}
        _require(isinstance(submission, dict), "dstack_response_invalid")
        provisioning = submission.get("job_provisioning_data") or {}
        _require(isinstance(provisioning, dict), "dstack_response_invalid")
        _require(provisioning.get("backend") == binding["backend"], "dstack_backend_binding_mismatch")
        resource = provisioning.get("instance_type", {}).get("resources", {})
        _require(isinstance(resource, dict) and isinstance(resource.get("gpus"), list)
            and len(resource["gpus"]) == 1, "dstack_allocated_gpu_count_unconfirmed")
        instance = provisioning.get("instance_id")
        _require(isinstance(instance, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}", instance),
            "dstack_instance_identity_unconfirmed")
        if binding.get("provider_instance_id"):
            _require(instance == binding["provider_instance_id"], "dstack_instance_identity_changed")
        value["provider_instance_id"] = instance
        if binding.get("stop_started"):
            # The original stop already fenced/retired inference. Keep fresh
            # provider identity evidence without bootstrapping/reopening it.
            value.update(state="stopping", reason_code=self._stop_retry_reason(binding))
            return value
        value["state"] = "runtime_unconfirmed"
        if self.readiness is None:
            return value
        # Commit the exact provider identity before the CPU native callback can
        # journal bootstrap. A timeout must reconcile this original instance.
        self.store.record(binding["intent_id"], value)
        binding = self.store.load(binding["intent_id"])
        try:
            native = self.readiness(copy.deepcopy(binding), copy.deepcopy(run))
        except Exception:
            value["reason_code"] = "dstack_native_observation_failed"
            return value
        _require(isinstance(native, HostReadiness) and native.slot_key == binding["intent_id"]
            and native.manifest_digest == binding["manifest_digest"]
            and type(native.idle) is bool
            and isinstance(native.incarnation, str) and re.fullmatch(r"[0-9a-f]{32}", native.incarnation),
            "dstack_native_binding_mismatch")
        if binding.get("runtime_incarnation"):
            _require(native.incarnation == binding["runtime_incarnation"], "dstack_native_incarnation_changed")
        value.update(state="ready" if native.idle else "busy", ready=native.idle,
            runtime_incarnation=native.incarnation,
            reason_code="dstack_runtime_ready_model_load_unobserved" if native.idle else "dstack_runtime_busy")
        return value

    def _stop_retry_reason(self, binding):
        count, due = binding.get("stop_attempt_count"), binding.get("stop_next_retry_at")
        if (type(count) is not int or count < 1 or type(due) not in (int, float)
                or not math.isfinite(due) or due <= 0):
            return "dstack_stop_retry_journal_unconfirmed"
        if count >= 5:
            return "dstack_stop_retry_exhausted"
        return "dstack_stop_retry_waiting" if self.clock() < due else "dstack_stop_retry_due"

    def stop(self, intent_id):
        binding = self.store.load(intent_id)
        _require(isinstance(binding, dict) and binding.get("run_id"), "dstack_owned_run_id_required")
        if binding.get("stop_started"):
            # dstack 0.22.3 stop_runs only moves existing nonfinished runs to
            # TERMINATING under locks; repeating that stop never creates a run.
            # Source: dstack/_internal/server/services/runs/__init__.py:868-909
            # in upstream tag 0.22.3 (not the create/update apply endpoint).
            # An accepted lost response normally already reads TERMINATING.
            # Only a *new* exact-owned positive RUNNING read can retry a stop
            # that was never delivered, after the original ledger's due/count
            # CAS. Cached RUNNING, absent/unknown and terminal reads cannot.
            observation = self.observe(intent_id)
            if (observation.get("dstack_status") != "running"
                    or observation.get("run_id") != binding["run_id"]
                    or not binding.get("provider_instance_id")
                    or observation.get("provider_instance_id") != binding["provider_instance_id"]):
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
                return {**observation, "reason_code": reason if reason != "dstack_stop_retry_due"
                    else "dstack_business_obligations_pending"}
        else:
            run = self.client.get(run_id=binding["run_id"])
            _require(run is not None, "dstack_stop_identity_unconfirmed")
            _owns(run, binding, self.client.project)
            if run.get("status") in TERMINAL | {"terminating", "stopping"}:
                value = self._observation(run, binding)
                self.store.record(intent_id, value)
                return value
            if not self.store.begin_stop(intent_id, binding["run_id"]):
                return {"state": "draining", "ready": False, "reason_code": "dstack_business_obligations_pending",
                    "observed_at": self.clock()}
        try:
            self.client.stop(binding["run_name"])
            value = {"state": "stopping", "ready": False, "reason_code": "dstack_stop_pending", "observed_at": self.clock()}
        except DstackError:
            value = {"state": "removal_unknown", "ready": False, "reason_code": "dstack_stop_requires_reconciliation", "observed_at": self.clock()}
        self.store.record(intent_id, value)
        return value


def idle_action(*, now, hard_deadline, hold_until, last_business_activity, idle_seconds,
                active_jobs, unsafe_attempts, collection_holds, observations_fresh):
    """Pure policy; stopping is still guarded transactionally by begin_stop.

    A running sleep command is not business-idle. max_duration starts after
    provisioning/image pull, so the controller also enforces absolute lifetime.
    """
    _require(all(type(v) in (int, float) and math.isfinite(v) and v >= 0
        for v in (now, hard_deadline, hold_until, last_business_activity, idle_seconds)), "dstack_idle_policy_invalid")
    _require(idle_seconds > 0 and all(type(n) is int and n >= 0
        for n in (active_jobs, unsafe_attempts, collection_holds)) and type(observations_fresh) is bool,
        "dstack_idle_policy_invalid")
    if not observations_fresh:
        return "reconcile"
    if active_jobs or unsafe_attempts or collection_holds:
        return "drain" if now >= hard_deadline else "retain"
    if now >= hard_deadline:
        return "stop"
    if now < hold_until:
        return "retain"
    return "stop" if now - last_business_activity >= idle_seconds else "retain"


def native_bootstrap_config(request, *, source_bundle_sha256,
                            prepared_root="/opt/workspace-internal/Wan2GP",
                            model_root="/root/sixnine-cache/models"):
    """Render the unchanged native bootstrap interface, without any credentials.

    The CPU controller must upload its exact bound bundle/manifest and a unique
    private runtime token, commit bootstrap intent, then invoke the original
    one-shot bootstrap once. A timeout re-observes that original operation.
    Source and environment checks remain in wangp_profile_bootstrap.prepare.
    """
    _require(isinstance(source_bundle_sha256, str) and SHA256.fullmatch(source_bundle_sha256),
        "dstack_source_bundle_digest_invalid")
    for path in (prepared_root, model_root):
        _require(isinstance(path, str) and PurePosixPath(path).is_absolute()
            and not any(part in {"..", "."} for part in PurePosixPath(path).parts)
            and re.fullmatch(r"/[A-Za-z0-9_./-]+", path), "dstack_runtime_path_invalid")
    base = "/workspace/h3-studio/profile-slot-0"
    return {"version": 1, "install_root": "/root/sixnine-cache/operator/profile-slot-0",
        "source_bundle_path": base + "/wangp-package.tar.gz", "source_bundle_sha256": source_bundle_sha256,
        "dependency_artifact_url": "", "dependency_artifact_path": "", "dependency_artifact_sha256": "0" * 64,
        "manifest_path": base + "/wangp-manifest.json", "model_root": model_root,
        "config_path": "/root/sixnine-cache/operator/profile-slot-0/wgp_config.json",
        "status_path": base + "/setup-status.json",
        "port": 8199, "prepared_root": prepared_root, "deployment_profile_id": request.profile_id,
        "profile_slot_index": 0, "expected_host_gpus": 1}


def main(argv=None):
    """Trusted CPU operations CLI. Persistence is the original service factory."""
    parser = argparse.ArgumentParser(description="Sixnine dstack capacity operations (0.22.3)")
    parser.add_argument("action", choices=("spec", "bootstrap-config", "plan", "start", "observe", "stop"))
    parser.add_argument("--request", type=Path, required=True, help="Frozen, non-secret CapacityRequest JSON")
    parser.add_argument("--ssh-public-key", type=Path)
    parser.add_argument("--source-bundle-sha256")
    parser.add_argument("--factory", help="Existing-ledger studio_platform.module:factory; takes no arguments")
    args = parser.parse_args(argv)
    try:
        _require(args.request.is_absolute() and not args.request.is_symlink()
            and args.request.stat().st_size <= 16384, "dstack_request_file_invalid")
        values = json.loads(args.request.read_text(encoding="utf-8"))
        _require(isinstance(values, dict), "dstack_request_file_invalid")
        for key in ("gpu_names", "regions"):
            if key in values:
                _require(isinstance(values[key], list), "dstack_request_file_invalid")
                values[key] = tuple(values[key])
        request = CapacityRequest(**values)
        public_key = None
        if args.action in {"spec", "plan", "start"}:
            _require(args.ssh_public_key is not None and args.ssh_public_key.is_absolute()
                and not args.ssh_public_key.is_symlink() and args.ssh_public_key.stat().st_size <= 16384,
                "dstack_public_ssh_key_invalid")
            public_key = args.ssh_public_key.read_text(encoding="utf-8").strip()
        if args.action == "spec":
            result = request.run_spec(public_key)
        elif args.action == "bootstrap-config":
            result = native_bootstrap_config(request, source_bundle_sha256=args.source_bundle_sha256)
        else:
            _require(isinstance(args.factory, str) and re.fullmatch(
                r"studio_platform\.[A-Za-z0-9_]+:[A-Za-z0-9_]+", args.factory), "dstack_existing_store_factory_required")
            module, function = args.factory.split(":")
            capacity = getattr(importlib.import_module(module), function)()
            _require(isinstance(capacity, DstackCapacity), "dstack_capacity_factory_invalid")
            if args.action == "plan":
                plan = capacity.plan(request, public_key)
                plans = plan.get("job_plans", [])
                result = {"run_name": request.run_name, "backend": request.backend,
                    "profile_id": request.profile_id, "mode": request.mode,
                    "total_offers": sum(p.get("total_offers", 0) for p in plans),
                    "current_resource_exists": plan.get("current_resource") is not None,
                    "creation_started": False}
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
