"""Explicit finite production Lium controller. Default: validate only, no IO to providers.

This process never initializes budgets/capacity or extends an approval. The API,
workers and controller use the same PostgreSQL and local object store. SIGTERM
closes admission, not reconciliation; unresolved work deliberately keeps it alive.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, replace
import hashlib
import json
import math
import os
from pathlib import Path
import re
import signal
import stat
import sys
import tempfile
import time
import uuid

from sqlalchemy import and_, or_, select, update

from .autoscale import Demand, ScalePolicy, Slot, recommend
from .capacity import ColdStartCoordinator, proven_unsubmitted_capacity_job
from .control import WorkerControl
from .execution_policy import ExecutionPolicies, read_policy, reservation_for_duration
from .lium_provider import BASE_URL, KEY_VARIABLE, PROFILE, SERVICE, LiumManifest, LiumProvider
from .lium_runtime_aws import AwsLiumLoader, SECRET_ARN, SECRET_NAME, VERSION_ID
from .repository import (Repository, Scope, attempts, budget_accounts, capacity_approvals,
    capacity_cycles, capacity_gate, capacity_waiters, instance_intents, jobs, registered_workers, request_hash, scaler_actions, scaler_receipts)
from .scaler import LaunchSpec, ScaleCoordinator
from .settings import Settings
from .worker import _slot_lock

MODEL = "MiniMax-H3-Base-BF16"
from .qualification_profiles import (FL_RECIPE, FL50_PROFILE, MULTIMODAL_PROFILE, QUEUED_TASK_PROFILE, RUNTIME_PROFILES, PROFILE_RECIPES,
    MULTIMODAL_INPUT_LIMITS, MIN_MULTIMODAL_JOB_RUNTIME_S)

RECIPE = FL_RECIPE  # Compatibility for existing exact FL50 operator configs.
IDENTIFIER = re.compile(r"[A-Za-z0-9_.:-]{1,120}")
HASH = re.compile(r"[0-9a-f]{64}")


class ScalerError(ValueError):
    """Static codes only; never interpolate provider/config/credential values."""


def unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ScalerError("duplicate_json_field")
        result[key] = value
    return result


def save(path, value):
    """Publish a private receipt without inheriting a permissive process umask."""
    path = Path(path)
    descriptor, name = tempfile.mkstemp(prefix="."+path.name+".", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        # mkstemp creates a new owner-only inode before any receipt bytes are
        # written. Each replacement stays private even if the old file is 0644.
        with os.fdopen(descriptor, "w", encoding="utf-8") as out:
            json.dump(value, out, sort_keys=True)
            out.flush()
            os.fsync(out.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


@dataclass(frozen=True)
class FiniteConfig:
    version: int
    enabled: bool
    cycle_id: str
    tenant: str
    owner: str
    project_id: str
    pool: str
    configuration_id: str
    capacity_approval_id: str
    budget_account_ids: list[str]
    created_at: float
    hard_deadline: float
    drain_margin_s: int
    collection_margin_s: int
    work_dir: Path
    data_dir: Path
    source_dir: Path
    ssh_key_file: Path
    known_hosts_file: Path
    trust_first_host_key: bool
    port_start: int
    source_sha256: dict
    execution_policy_sha256: str
    qualification_evidence_id: str
    secret_arn: str
    secret_version_id: str
    scale_policy: dict
    launches: list[dict]
    manifests: list[dict]
    interval_s: int = 15
    allowed_owners: list[str] | None = None
    qualification_profile: str = FL50_PROFILE
    authorization_extension_s: int = 0
    execution_backend: str = "comfy-worker"
    engine_manifest_digest: str = ""
    service_policy: dict | None = None
    output_delivery: str = ""
    provider_preparation_timeout_s: int | None = None
    provider_preparation_failure_limit: int = 2
    execution_recipe_id: str = ""

    def __post_init__(self):
        if (self.execution_recipe_id and (self.execution_backend != "wangp-worker"
                or self.execution_recipe_id != "h3-base-ref2va-v1" or self.output_delivery != "native-frames-v1")
                or not isinstance(self.execution_recipe_id, str)):
            raise ScalerError("finite_explicit_recipe_invalid")
        if self.provider_preparation_timeout_s is not None and (type(self.provider_preparation_timeout_s) is not int
                or not 120 <= self.provider_preparation_timeout_s <= 7200):
            raise ScalerError("invalid_provider_preparation_timeout")
        if (type(self.provider_preparation_failure_limit) is not int
                or not 1 <= self.provider_preparation_failure_limit <= 5
                or self.provider_preparation_timeout_s is None and self.provider_preparation_failure_limit != 2):
            raise ScalerError("invalid_provider_preparation_failure_limit")
        from .inference.outputs import validate_delivery_policy
        validate_delivery_policy(self.execution_backend, self.output_delivery)
        if self.execution_backend not in {"comfy-worker", "wangp-worker"}:
            raise ScalerError("finite_backend_invalid")
        if self.execution_backend == "wangp-worker":
            if (not isinstance(self.engine_manifest_digest, str) or not HASH.fullmatch(self.engine_manifest_digest)
                    or self.qualification_profile != QUEUED_TASK_PROFILE):
                raise ScalerError("finite_wangp_identity_required")
        elif self.engine_manifest_digest:
            raise ScalerError("finite_unexpected_engine_manifest")
        if self.service_policy is not None:
            from .service_policy import validate_service_config
            validate_service_config(self)
        if not isinstance(self.qualification_profile, str) or self.qualification_profile not in PROFILE_RECIPES:
            raise ScalerError("finite_qualification_profile_invalid")
        if (type(self.version) is not int or self.version != 1 or type(self.enabled) is not bool
                or self.service_policy is None and (self.tenant != "sixnine" or self.owner not in ("superdan", "supervan"))
                or type(self.trust_first_host_key) is not bool):
            raise ScalerError("finite_identity_invalid")
        if self.service_policy is None and self.allowed_owners is not None and (not isinstance(self.allowed_owners, list)
                or len(self.allowed_owners) != 2
                or any(not isinstance(owner, str) for owner in self.allowed_owners)
                or set(self.allowed_owners) != {"superdan", "supervan"}):
            raise ScalerError("finite_shared_owners_invalid")
        if (type(self.authorization_extension_s) is not int or self.authorization_extension_s not in (0, 18000)
                or self.authorization_extension_s and (self.allowed_owners is None
                    or self.qualification_profile != QUEUED_TASK_PROFILE)):
            raise ScalerError("finite_authorization_extension_invalid")
        for value in (self.cycle_id, self.project_id, self.pool, self.configuration_id,
                      self.capacity_approval_id, self.qualification_evidence_id):
            if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
                raise ScalerError("finite_identifier_invalid")
        for field in ("work_dir", "data_dir", "source_dir", "ssh_key_file", "known_hosts_file"):
            path = Path(getattr(self, field))
            if not path.is_absolute():
                raise ScalerError("finite_absolute_paths_required")
            object.__setattr__(self, field, path)
        if (any(type(x) not in (int, float) or not math.isfinite(x) for x in (self.created_at, self.hard_deadline))
                or self.service_policy is None and not 0 < self.hard_deadline-self.created_at <= (
                    (86400 if self.allowed_owners is not None else 14400) + self.authorization_extension_s)
                or type(self.drain_margin_s) is not int or not 120 <= self.drain_margin_s <= 3600
                or type(self.collection_margin_s) is not int or not 30 <= self.collection_margin_s <= 900
                or type(self.interval_s) is not int or not 5 <= self.interval_s <= 30
                or type(self.port_start) is not int or not 1024 <= self.port_start <= 65533):
            raise ScalerError("finite_deadline_or_limits_invalid")
        if (not isinstance(self.source_sha256, dict)
                or set(self.source_sha256) != self.source_names
                or any(not isinstance(v, str) or not HASH.fullmatch(v) for v in self.source_sha256.values())
                or not isinstance(self.execution_policy_sha256, str) or not HASH.fullmatch(self.execution_policy_sha256)
                or not isinstance(self.secret_arn, str) or not SECRET_ARN.fullmatch(self.secret_arn)
                or not isinstance(self.secret_version_id, str) or not VERSION_ID.fullmatch(self.secret_version_id)):
            raise ScalerError("finite_pinned_sources_required")
        if (not isinstance(self.budget_account_ids, list) or not 1 <= len(self.budget_account_ids) <= 8
                or len(set(self.budget_account_ids)) != len(self.budget_account_ids)
                or any(not isinstance(v, str) or not IDENTIFIER.fullmatch(v) for v in self.budget_account_ids)):
            raise ScalerError("finite_existing_budget_ids_required")
        try:
            from .service_policy import service_member_ids
            member_ids = service_member_ids(self)
            policy = ScalePolicy(**self.scale_policy)
            recommend([], [], [], now=self.created_at, policy=policy)
            launches = [LaunchSpec(**v) for v in self.launches]
            manifests = [LiumManifest(**v) for v in self.manifests]
            if (policy.dry_run is not False or policy.hard_deadline != self.hard_deadline
                    or not 1 <= policy.max_instances <= 2 or policy.max_physical_gpus != policy.max_instances
                    or policy.new_instance_slots != 1 or policy.new_instance_physical_gpus != 1
                    or not policy.approved_remaining_microusd or not policy.instance_reservation_microusd
                    or len(launches) != (1 if member_ids else policy.max_instances) or len(manifests) != len(launches)
                    or len({v.offer_id for v in launches}) != len(launches)):
                raise ValueError
            for launch, manifest in zip(launches, manifests):
                if (launch.provider != "lium" or launch.configuration_id != self.configuration_id or launch.model_id != MODEL
                        or manifest.configuration_id != self.configuration_id or manifest.model_id != MODEL
                        or manifest.executor_id != launch.offer_id or manifest.template_id != launch.image_id
                        or manifest.region != launch.region or manifest.gpu_count != 1 or manifest.execution_slots != 1
                        or manifest.minimum_vram_mib and manifest.minimum_vram_mib < 30*1024
                        or not 1 <= manifest.termination_hours <= 4 or manifest.approved_until > self.hard_deadline
                        or manifest.approved_until <= self.created_at or not manifest.allow_preflight_only_price_cap
                        or policy.instance_reservation_microusd < manifest.max_price_per_gpu_hour_microusd*manifest.termination_hours):
                    raise ValueError
        except Exception:
            raise ScalerError("finite_launch_or_budget_manifest_invalid") from None

    @property
    def scope(self):
        return Scope(self.tenant, self.owner, self.project_id)

    @property
    def recipe_ids(self):
        if self.execution_backend == "wangp-worker":
            return (self.execution_recipe_id or FL_RECIPE,)
        return PROFILE_RECIPES[self.qualification_profile]

    @property
    def source_names(self):
        if self.execution_backend == "wangp-worker":
            from .wangp_bootstrap import SOURCE_NAMES
            return SOURCE_NAMES
        return {"bootstrap_cloud.py", "model_manifest.json"}

    @property
    def stop_claiming_at(self):
        return self.hard_deadline-self.drain_margin_s

    def fingerprint(self):
        value = asdict(self)
        # Preserve the legacy exact-story configuration identity. Shared scope
        # is an explicit opt-in and must be part of its new immutable identity.
        if self.allowed_owners is None:
            value.pop("allowed_owners")
        if self.qualification_profile == FL50_PROFILE:
            value.pop("qualification_profile")
        # Absent/default authorization keeps all historical config hashes.
        # The explicitly authorized five-hour extension is immutable identity.
        if self.authorization_extension_s == 0:
            value.pop("authorization_extension_s")
        if self.execution_backend == "comfy-worker":
            value.pop("execution_backend")
            value.pop("engine_manifest_digest")
        if self.service_policy is None:
            value.pop("service_policy")
        if not self.output_delivery:
            value.pop("output_delivery")
        if self.provider_preparation_timeout_s is None:
            value.pop("provider_preparation_timeout_s")
        if self.provider_preparation_failure_limit == 2:
            value.pop("provider_preparation_failure_limit")
        if not self.execution_recipe_id:
            value.pop("execution_recipe_id")
        for key in ("work_dir", "data_dir", "source_dir", "ssh_key_file", "known_hosts_file"):
            value[key] = str(value[key])
        return request_hash(value)


def job_scope_filter(config):
    """Same queue boundary in the parent and child; absent opt-in stays exact."""
    identity = (jobs.c.owner_id.in_(config.allowed_owners) if config.allowed_owners is not None
        else and_(jobs.c.owner_id == config.owner, jobs.c.project_id == config.project_id))
    return and_(jobs.c.tenant_id == config.tenant, identity, jobs.c.pool == config.pool,
        jobs.c.execution_plan["configuration_id"].as_string() == config.configuration_id)


def job_scope_allowed(config, job):
    return bool(job["tenant_id"] == config.tenant and job["pool"] == config.pool
        and job["execution_plan"].get("configuration_id") == config.configuration_id
        and (job["owner_id"] in config.allowed_owners if config.allowed_owners is not None
             else job["owner_id"] == config.owner and job["project_id"] == config.project_id))


def read_config(path):
    try:
        path = Path(path)
        if not path.is_absolute() or path.is_symlink():
            raise ValueError
        with path.open("rb") as source:
            info = os.fstat(source.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or os.name != "nt" and info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)):
                raise ValueError
            raw = source.read(65537)
        if len(raw) > 65536:
            raise ValueError
        return FiniteConfig(**json.loads(raw, object_pairs_hook=unique))
    except Exception:
        raise ScalerError("finite_config_unavailable_or_invalid") from None


def stdin_loader(config, stream):
    """One bounded host-to-container pipe; no environment/file/cache fallback."""
    try:
        raw = stream.read(24577)
        if len(raw) > 24576:
            raise ValueError
        envelope = json.loads(raw, object_pairs_hook=unique)
        if (not isinstance(envelope, dict) or set(envelope) != {"secret_arn", "version_id", "payload"}
                or envelope["secret_arn"] != config.secret_arn or envelope["version_id"] != config.secret_version_id):
            raise ValueError
        # Reuse the reviewed metadata/key boundary without constructing boto3.
        class MemorySecret:
            def get_secret_value(self, **kwargs):
                return {"ARN": config.secret_arn, "Name": SECRET_NAME, "VersionId": config.secret_version_id,
                        "SecretString": json.dumps(envelope["payload"])}
            def close(self):
                envelope.clear()
        loader = AwsLiumLoader(config.secret_arn, config.secret_version_id, client_factory=MemorySecret)
        loader(SERVICE, profile=PROFILE)
        return loader
    except Exception:
        raise ScalerError("finite_credential_envelope_invalid") from None


def validate_settings(config, settings, *, require_policy=True):
    if (not settings.database_url.startswith("postgresql+psycopg:") or settings.tenant_id != config.tenant
            or settings.data_dir != config.data_dir.resolve() or settings.storage_provider != "local"
            or settings.auth_mode != "password" or settings.public_origin not in {"https://inkseq.com", "https://www.sixnine.art"}
            or not settings.generation_enabled or settings.execution_backend != config.execution_backend):
        raise ScalerError("finite_requires_same_production_postgres_and_local_store")
    if require_policy:
        verify_policy(config, settings)


def verify_policy(config, settings):
    policy = read_policy(settings.execution_policy_file)
    if (not policy or request_hash(policy) != config.execution_policy_sha256 or policy["pool"] != config.pool
            or policy["backend"] != config.execution_backend
            or policy.get("engine_manifest_digest", "") != config.engine_manifest_digest
            or policy.get("output_delivery", "") != config.output_delivery
            or policy["configuration_id"] != config.configuration_id or policy["recipe_ids"] != list(config.recipe_ids)
            or policy["qualification"]["status"] != ("runtime_required" if config.qualification_profile in RUNTIME_PROFILES else "accepted")
            or policy["qualification"]["evidence_id"] != config.qualification_evidence_id
            or policy["qualification"]["expires_at"] > config.hard_deadline
            or policy["reservation"]["expires_at"] > config.hard_deadline):
        raise ScalerError("finite_policy_identity_mismatch")
    # An explicit profile selects the entire qualification suite; no recipe or
    # input family is enabled just because the underlying model supports it.
    envelope = policy["envelope"]
    if config.execution_backend == "wangp-worker":
        if config.execution_recipe_id:
            # read_policy has already checked the exact REF/native envelope.
            from .inference.wangp_ref_compiler import validate_envelope
            try:
                validate_envelope(envelope)
            except ValueError:
                raise ScalerError("finite_policy_outside_wangp_ref_recipe") from None
            return policy
        controls = {"sampler_name": ["euler"], "scheduler": ["auto"], "video_decode": ["tiled"],
                    "audio_decode": ["normal"], "encoder_device": ["default"]}
        limits = envelope.get("input_limits", {})
        if (envelope["controls"] != controls or envelope["max_pixels"] > 1344*768
                or envelope["max_steps"] != 50 or envelope["max_duration_seconds"] > 362/24
                or envelope["max_reference_files"] > (2 if envelope["allow_first_last"] else 0)
                or envelope["max_guides"] != 0
                or any(limits.get(key) != 0 for key in ("max_images", "max_videos", "max_audios"))
                or limits.get("guide_kinds") != [] or limits.get("guide_recipe_ids") != []
                or limits.get("allow_video_audio") is not False
                or policy["qualification"].get("profile") != QUEUED_TASK_PROFILE):
            raise ScalerError("finite_policy_outside_wangp_recipe")
        return policy
    controls = {"sampler_name": ["res_multistep"], "scheduler": ["auto"], "video_decode": ["tiled"],
                "audio_decode": ["normal"], "encoder_device": ["cpu"], "ref_image_size": ["max"]}
    # Legacy synthetic suites qualified five-second output only. Their bound
    # stays unchanged. The explicit real-task profile may admit the full native
    # 4--15-second request range; readiness is never a claim that it has already
    # generated that duration. Fifteen seconds snaps to 362 frames / 24fps.
    maximum_duration = 362/24 if config.qualification_profile == QUEUED_TASK_PROFILE else 6
    if (envelope["max_pixels"] > 1344*768 or envelope["max_duration_seconds"] > maximum_duration or envelope["max_steps"] > 50
            or envelope["controls"] != controls):
        raise ScalerError("finite_policy_outside_qualified_fl50_envelope")
    if config.qualification_profile == FL50_PROFILE:
        if envelope["max_reference_files"] != 0 or envelope["max_guides"] != 0 or envelope["allow_first_last"]:
            raise ScalerError("finite_policy_outside_qualified_fl50_envelope")
    else:
        limits = envelope.get("input_limits")
        if (policy["qualification"].get("profile") != config.qualification_profile
                or not isinstance(limits, dict) or set(limits) != set(MULTIMODAL_INPUT_LIMITS)
                or envelope["max_reference_files"] > 3 or envelope["max_guides"] > 1
                or policy["reservation"]["expected_runtime_s"] < MIN_MULTIMODAL_JOB_RUNTIME_S):
            raise ScalerError("finite_policy_outside_qualified_multimodal_envelope")
        for field, maximum in MULTIMODAL_INPUT_LIMITS.items():
            actual = limits[field]
            if isinstance(maximum, list):
                valid = isinstance(actual, list) and set(actual) <= set(maximum)
            elif type(maximum) is bool:
                valid = type(actual) is bool and (not actual or maximum)
            else:
                valid = type(actual) in (int, float) and math.isfinite(actual) and 0 <= actual <= maximum
            if not valid:
                raise ScalerError("finite_policy_outside_qualified_multimodal_envelope")
    return policy


def verify_sources(config):
    if config.execution_backend == "wangp-worker":
        from .wangp_bootstrap import read_sources
        files, _ = read_sources(config)
        if {name: hashlib.sha256(data).hexdigest() for name, data in files.items()} != config.source_sha256:
            raise ScalerError("finite_public_source_hash_mismatch")
        return
    for name, expected in config.source_sha256.items():
        path = config.source_dir/name
        maximum = 524288 if name.endswith(".py") else 65536
        if (path.is_symlink() or not path.is_file() or path.stat().st_size > maximum
                or hashlib.sha256(path.read_bytes()).hexdigest() != expected):
            raise ScalerError("finite_public_source_hash_mismatch")
    from .lium_bootstrap import COMFY_REVISION, MODEL_REVISION
    try:
        manifest = json.loads((config.source_dir/"model_manifest.json").read_text(encoding="utf-8"))
        if (manifest.get("revision") != MODEL_REVISION or manifest.get("comfyui_revision") != COMFY_REVISION
                or manifest.get("repository") != "Comfy-Org/MiniMax-H3" or len(manifest.get("files", [])) != 5):
            raise ValueError
    except Exception:
        raise ScalerError("finite_public_model_manifest_mismatch") from None


def verify_identity_files(config):
    """Only metadata here; SSHHost is the sole reader of the private key."""
    try:
        key = config.ssh_key_file
        meta = key.lstat()
        if (not stat.S_ISREG(meta.st_mode) or meta.st_nlink != 1
                or os.name != "nt" and meta.st_mode & (stat.S_IRWXG | stat.S_IRWXO)):
            raise ValueError
        for path in (config.work_dir, key.parent, config.known_hosts_file.parent):
            if any(parent.is_symlink() for parent in (path, *path.parents)):
                raise ValueError
        if config.known_hosts_file.exists():
            meta = config.known_hosts_file.lstat()
            if (not stat.S_ISREG(meta.st_mode) or meta.st_nlink != 1
                    or os.name != "nt" and meta.st_mode & (stat.S_IWGRP | stat.S_IWOTH)):
                raise ValueError
        elif not config.trust_first_host_key:
            raise ValueError
        if config.trust_first_host_key:
            config.known_hosts_file.relative_to(config.work_dir)
    except Exception:
        raise ScalerError("finite_ssh_identity_or_host_key_path_invalid") from None


class FiniteController:
    def __init__(self, repo, settings, config, *, provider, scaler=None, boot_factory=None):
        self.repo, self.settings, self.config, self.provider = repo, settings, config, provider
        self.scaler = scaler or ScaleCoordinator(repo, provider=provider, enabled=True, leader_seconds=180,
            preparation_timeout_s=config.provider_preparation_timeout_s,
            preparation_binding=config.fingerprint())
        self.policies = ExecutionPolicies(settings, repo)
        self.cold = ColdStartCoordinator(repo, scaler=self.scaler, enabled=True,
            approval_guard=self.approval_current, activation_guard=self.job_allowed)
        self.boot_factory = boot_factory
        self.boots = {}
        self.leader_id = "finite-"+uuid.uuid4().hex  # Never share one live leader identity across hosts.
        self.last_error = None

    def stopping(self):
        return self.repo.clock() >= self.config.stop_claiming_at or (self.config.work_dir/"drain.flag").exists()

    def scope_filter(self):
        return job_scope_filter(self.config)

    def job_allowed(self, job):
        return bool(job_scope_allowed(self.config, job) and self.policies.activation_allowed(job))

    def approval_current(self, payload):
        c = self.config
        return bool("pool_members" not in payload and not self.stopping()
            and payload["tenant_id"] == c.tenant and payload["pool"] == c.pool
            and payload["configuration_id"] == c.configuration_id and payload["recipe_ids"] == list(c.recipe_ids)
            and payload["policy_hash"] == c.execution_policy_sha256
            and payload["budget_scope"] == asdict(c.scope)
            and sorted(payload["budget_account_ids"]) == sorted(c.budget_account_ids)
            and payload["scale_policy"] == c.scale_policy and payload["launch"] == c.launches[0]
            and self.policies.capacity_approval_current(payload))

    def create_allowed(self):
        if self.stopping():
            return False
        try:
            with self.repo.engine.connect() as conn:
                approval = conn.execute(select(capacity_approvals).where(
                    capacity_approvals.c.id == self.config.capacity_approval_id)).mappings().one()
            return bool(approval["enabled"] == 1 and approval["expires_at"] > self.repo.clock()
                and self.approval_current(approval["payload"]))
        except Exception:
            return False

    def initialize(self):
        c = self.config
        c.work_dir.mkdir(parents=True, exist_ok=True)
        path = c.work_dir/"cycle-state.json"
        if path.exists():
            receipt = json.loads(path.read_text())
            if receipt.get("config_hash") != c.fingerprint():
                raise ScalerError("finite_cycle_configuration_changed")
        else:
            if self.repo.list_instance_intents(pool=c.pool):
                raise ScalerError("finite_new_cycle_requires_unused_pool")
            if not c.created_at <= self.repo.clock() < c.stop_claiming_at:
                raise ScalerError("finite_approval_time_unavailable")
            save(path, {"config_hash": c.fingerprint(), "ports": {}, "created_at": self.repo.clock()})
        with self.repo.engine.connect() as conn:
            approval = conn.execute(select(capacity_approvals).where(capacity_approvals.c.id == c.capacity_approval_id)).mappings().first()
        if not approval:
            raise ScalerError("finite_capacity_approval_mismatch")
        # A restarted existing cycle must retain its cleanup path when an
        # operator revoked/replaced admission policy. It cannot submit again.
        if not self.approval_current(approval["payload"]) or approval["enabled"] != 1:
            self.request_drain()
        self.remaining_budget()  # Existing accounts only; never creates/raises them.

    def remaining_budget(self):
        c = self.config
        with self.repo.engine.connect() as conn:
            accounts = list(conn.execute(select(budget_accounts).where(budget_accounts.c.id.in_(c.budget_account_ids))).mappings())
        if len(accounts) != len(c.budget_account_ids) or any(a["tenant_id"] != c.tenant
                or a["owner_id"] not in (None, c.owner) or a["project_id"] not in (None, c.project_id) for a in accounts):
            raise ScalerError("finite_existing_budget_scope_mismatch")
        if c.service_policy is not None:
            ceiling = c.service_policy["budget_ceiling_microusd"]
            return min(max(0, min(a["limit_microusd"], ceiling)-a["spent_microusd"]-a["reserved_microusd"])
                       for a in accounts)
        return min(c.scale_policy["approved_remaining_microusd"],
                   *(max(0, a["limit_microusd"]-a["spent_microusd"]-a["reserved_microusd"]) for a in accounts))

    def port_for(self, intent_id):
        path = self.config.work_dir/"cycle-state.json"
        receipt = json.loads(path.read_text())
        if receipt.get("config_hash") != self.config.fingerprint():
            raise ScalerError("finite_cycle_configuration_changed")
        ports = receipt.get("ports")
        if (not isinstance(ports, dict) or len(set(ports.values())) != len(ports)
                or any(type(v) is not int or not self.config.port_start <= v < self.config.port_start+len(self.config.launches)
                       for v in ports.values())):
            raise ScalerError("finite_port_mapping_invalid")
        if intent_id not in ports:
            available = [v for v in range(self.config.port_start, self.config.port_start+len(self.config.launches)) if v not in ports.values()]
            if not available:
                raise ScalerError("finite_cycle_instance_count_exhausted")
            ports[intent_id] = available[0]
            save(path, receipt)
        return ports[intent_id]

    def _managed(self):
        rows = self.repo.list_instance_intents(pool=self.config.pool)
        with self.repo.engine.connect() as conn:
            actions = {r["intent_id"]: dict(r) for r in conn.execute(select(scaler_actions).where(scaler_actions.c.pool == self.config.pool)).mappings()}
        for row in rows:
            action = actions.get(row["id"])
            if row["provider"] != "lium" or action is None:
                raise ScalerError("finite_pool_contains_unapproved_intent")
            if action["launch_spec"] in self.config.launches:
                continue
            with self.repo.engine.connect() as conn:
                receipts = list(conn.execute(select(scaler_receipts.c.facts).where(
                    scaler_receipts.c.intent_id == row["id"],
                    scaler_receipts.c.operation == "operator-audit")).scalars())
            # An exceptional, fenced handoff may replace a stale selector only
            # after proving this exact old request was never submitted. Never
            # rewrite its historical action or overlook an allocated instance.
            settled = row["state"] == "destroyed" and row["provider_instance_id"] is None and row["billing_status"] == "settled"
            verified = any(f.get("handoff_verified") is True and f.get("state") == "not_created"
                and f.get("absence_confirmed") is True and f.get("actual_cost_microusd") == 0
                and f.get("configuration_id") == self.config.configuration_id
                and f.get("model_id") == MODEL
                and isinstance(f.get("old_config_hash"), str) and HASH.fullmatch(f["old_config_hash"])
                and isinstance(f.get("evidence_sha256"), str) and HASH.fullmatch(f["evidence_sha256"])
                and f.get("launch_spec_sha256") == request_hash(action["launch_spec"])
                for f in receipts if isinstance(f, dict))
            if not settled or not verified:
                raise ScalerError("finite_pool_contains_unapproved_intent")
        return rows, actions

    def _observations(self, instances):
        now, c = self.repo.clock(), self.config
        with self.repo.engine.connect() as conn:
            # Project/asset snapshots can be MiB each. Demand observations use
            # only scalar columns and exact immutable admission identities.
            pending = list(conn.execute(select(jobs.c.id, jobs.c.owner_id, jobs.c.created_at, jobs.c.expected_runtime_s).where(
                self.scope_filter(), jobs.c.status == "queued", jobs.c.not_before <= now,
                jobs.c.execution_plan["policy_hash"].as_string() == c.execution_policy_sha256,
                jobs.c.execution_plan["backend"].as_string() == c.execution_backend,
                jobs.c.execution_plan["enabled"].as_string() == ("true" if self.repo.engine.dialect.name == "postgresql" else 1),
                jobs.c.request["recipe_id"].as_string().in_(c.recipe_ids),
                jobs.c.request["request"]["model"].as_string() == MODEL
                ).order_by(jobs.c.created_at, jobs.c.id).limit(4097)).mappings())
            workers = list(conn.execute(select(registered_workers).where(registered_workers.c.pool == c.pool)).mappings())
            busy_job_ids = {w["current_job_id"] for w in workers if w["current_job_id"]}
            busy_runtimes = dict(conn.execute(select(jobs.c.id, jobs.c.expected_runtime_s).where(
                self.scope_filter(), jobs.c.id.in_(busy_job_ids))).all()) if busy_job_ids else {}
        if len(pending) > 4096:
            raise ScalerError("finite_demand_window_exceeded")
        demands = [Demand(r["id"], r["owner_id"], r["created_at"], r["expected_runtime_s"], "operator-reservation")
                   for r in pending]
        slots = []
        policy = read_policy(self.settings.execution_policy_file)
        # A busy long task owns its full admitted allowance, not the historical
        # five-second baseline. Missing bindings conservatively use the largest
        # operator allowance; old unscaled policies retain their original value.
        runtime = reservation_for_duration(policy, policy["envelope"]["max_duration_seconds"])["expected_runtime_s"]
        for w in workers:
            if (w["expires_at"] > now and not w["drain_requested"] and w["state"] in ("ready", "busy", "leased", "reconciling")
                    and w["spec"]["configuration_id"] == c.configuration_id
                    and any(i["provider_instance_id"] == w["instance_id"] and i["state"] in ("starting", "ready", "busy") for i in instances)):
                bound_runtime = busy_runtimes.get(w["current_job_id"])
                if not (type(bound_runtime) in (int, float) and math.isfinite(bound_runtime) and bound_runtime > 0):
                    bound_runtime = runtime
                slots.append(Slot(w["id"], "busy" if w["current_job_id"] else "ready",
                    bound_runtime if w["current_job_id"] else 0))
        for row in instances:
            if row["state"] == "starting" and not any(w["instance_id"] == row["provider_instance_id"] for w in workers):
                slots.append(Slot("starting-"+row["id"], "starting", c.scale_policy["cold_start_s"]))
        return demands, slots

    def idle_probe(self, tag, instance_id):
        if tag not in self.boots:
            raise ScalerError("finite_upstream_idle_not_observed")
        with self.repo.engine.connect() as conn:
            if conn.execute(self._unsafe_attempts(instance_id=instance_id).limit(1)).first():
                raise ScalerError("finite_attempt_still_unresolved")
        return self.boots[tag].idle_probe(tag, instance_id)

    def _unsafe_attempts(self, *, instance_id=None):
        terminal = ("succeeded", "failed", "cancelled")
        started = or_(attempts.c.submission_started_at.is_not(None), attempts.c.upstream_task_id.is_not(None))
        unsafe = or_(and_(attempts.c.id == jobs.c.current_attempt_id, attempts.c.status.not_in(terminal)),
            and_(started, or_(attempts.c.upstream_stopped != 1, attempts.c.status.not_in(terminal))))
        statement = select(attempts.c.job_id).join(jobs, jobs.c.id == attempts.c.job_id).join(
            registered_workers, registered_workers.c.id == attempts.c.worker_id).where(
            registered_workers.c.pool == self.config.pool, unsafe)
        if instance_id is not None:
            statement = statement.where(registered_workers.c.instance_id == instance_id)
        return statement

    def request_drain(self):
        (self.config.work_dir/"drain.flag").touch()
        for boot in self.boots.values():
            boot.request_drain()

    def _drain_intents(self, rows):
        for row in rows:
            if row["state"] in ("starting", "ready", "busy"):
                self.repo.update_instance(row["id"], "draining")
            if row["id"] in self.boots:
                self.boots[row["id"]].request_drain()

    def _close_unsubmitted(self):
        """Revoke only this cycle and settle proven unsubmitted jobs via existing APIs.

        Already submitted, unknown and held attempts keep their original
        recovery/collection path and reservation. Bounded pages are revisited.
        """
        self.repo.set_capacity_approval_enabled(self.config.capacity_approval_id, enabled=False)
        if self.config.qualification_profile == QUEUED_TASK_PROFILE:
            # A failing first user job must not cancel another user's accepted
            # backlog. Revocation forbids new work; old waits keep their exact
            # deadlines and reservations, without automatic replay or renewal.
            self.hold_queued_task_backlog()
            return
        self.cold.advance_once(self.config.capacity_approval_id)
        with self.repo.engine.connect() as conn:
            pending = list(conn.execute(select(jobs.c.id, jobs.c.tenant_id, jobs.c.owner_id, jobs.c.project_id).where(self.scope_filter(),
                jobs.c.status.in_(("planned", "blocked", "queued", "claimed", "waiting_capacity")))
                .order_by(jobs.c.created_at, jobs.c.id).limit(256)).mappings())
        for row in pending:
            # A simultaneous POST can change state before this call; the
            # repository then records cancel_requested/hold, never a false $0.
            from .job_cancellation import metadata_for
            self.repo.request_cancel(Scope(row["tenant_id"], row["owner_id"], row["project_id"]), row["id"],
                cancellation=metadata_for("system", "capacity_unavailable", None, self.repo.clock()))

    def hold_queued_task_backlog(self):
        """Preserve only this profile's proven-unsubmitted accepted jobs.

        This inert backlog publication never proves upstream idle, permits a
        replacement rental, or changes a submitted/unknown attempt. It is also
        used by the on-demand repair hold, including after process restart.
        """
        if self.config.qualification_profile != QUEUED_TASK_PROFILE:
            raise ScalerError("queued_task_backlog_profile_required")
        from .capacity import CAPACITY_WAIT_CODES
        code = "capacity_queued_task_repair_required"
        with self.repo.transaction() as conn:
            self.repo._locked(conn, select(capacity_gate).where(capacity_gate.c.id == "global"))
            ids = list(conn.execute(select(jobs.c.id).where(self.scope_filter(),
                jobs.c.status.in_(("queued", "waiting_capacity")),
                jobs.c.execution_plan["policy_hash"].as_string() == self.config.execution_policy_sha256)
                .order_by(jobs.c.id).limit(4097)).scalars())
            if len(ids) > 4096:
                raise ScalerError("finite_demand_window_exceeded")
            for jid in ids:
                job = self.repo._job(conn, jid, lock=True)
                if not proven_unsubmitted_capacity_job(conn, job):
                    continue
                waiter = conn.execute(select(capacity_waiters).where(
                    capacity_waiters.c.job_id == jid)).mappings().first()
                deadlines = [self.config.hard_deadline - job["expected_runtime_s"]]
                for field in ("qualification_expires_at", "quote_expires_at"):
                    value = job["execution_plan"].get(field)
                    if type(value) in (int, float) and math.isfinite(value):
                        deadlines.append(value)
                if waiter is not None:
                    deadlines.append(waiter["deadline"])
                if self.repo.clock() >= min(deadlines):
                    self.repo._settle(conn, "job", jid, 0)
                    conn.execute(update(jobs).where(jobs.c.id == jid).values(status="failed",
                        error_code="capacity_wait_deadline_expired", fence=job["fence"]+1,
                        updated_at=self.repo.clock()))
                    if waiter is not None:
                        conn.execute(update(capacity_waiters).where(capacity_waiters.c.job_id == jid).values(state="failed"))
                    self.repo._emit(conn, "job.failed", jid, {"job_id": jid, "status": "failed",
                        "error_code": "capacity_wait_deadline_expired"})
                elif job["error_code"] is None or job["error_code"] in CAPACITY_WAIT_CODES.values():
                    if job["error_code"] != code:
                        conn.execute(update(jobs).where(jobs.c.id == jid).values(error_code=code,
                            updated_at=self.repo.clock()))
                        self.repo._emit(conn, "job.capacity_wait", jid, {"job_id": jid,
                            "status": job["status"], "error_code": code})

    def _boot_failure(self, intent, state):
        """Finite runs stop; on-demand preparation recovery may preserve backlog."""
        self.request_drain()

    def _sync_provider_preparation(self):
        """Service cycles override this to retain backlog before capacity advance."""
        pass

    def _capacity_advance_allowed(self):
        return True

    def _bootstrap_start_allowed(self, intent_id, lease):
        """Fence upload completion before remote setup; never acquire ownership here."""
        from .repository import capacity_waiters
        try:
            if self.stopping():
                return False
            with self.repo.transaction() as conn:
                self.scaler._leader(conn, lease)
                intent = conn.execute(select(instance_intents).where(instance_intents.c.id == intent_id)).mappings().one()
                approval = conn.execute(select(capacity_approvals).where(
                    capacity_approvals.c.id == self.config.capacity_approval_id)).mappings().one()
                if (intent["pool"] != self.config.pool or intent["state"] not in {"starting", "ready", "busy"}
                        or intent["hard_deadline"]-self.repo.clock() < self.config.drain_margin_s
                        or not approval["enabled"] or approval["expires_at"] <= self.repo.clock()
                        or not self.approval_current(approval["payload"])):
                    return False
                if self.config.qualification_profile == QUEUED_TASK_PROFILE:
                    demand = conn.execute(select(jobs.c.id).join(capacity_waiters, capacity_waiters.c.job_id == jobs.c.id).where(
                        capacity_waiters.c.approval_id == approval["id"],
                        capacity_waiters.c.intent_id == intent_id,
                        capacity_waiters.c.state == "waiting_capacity", capacity_waiters.c.deadline > self.repo.clock(),
                        jobs.c.status.in_(("queued", "waiting_capacity")), self.scope_filter()).limit(1)).first()
                    if demand is None:
                        return False
                return True
        except Exception:
            return False

    def _capacity_decision(self, instances, actions, stopping):
        c = self.config
        used = {a["launch_spec"]["offer_id"] for a in actions.values()}
        launch = next((LaunchSpec(**v) for v in c.launches if v["offer_id"] not in used), None)
        demands, slots = ([], []) if stopping else self._observations(instances)
        with self.repo.engine.connect() as conn:
            cycle = conn.execute(select(capacity_cycles).where(capacity_cycles.c.approval_id == c.capacity_approval_id)).first()
        if not stopping and cycle is None and not instances:
            # Reject out-of-scope cold waiters rather than financing them.
            from .repository import capacity_waiters
            with self.repo.engine.connect() as conn:
                outside = conn.execute(select(jobs.c.id).join(capacity_waiters, capacity_waiters.c.job_id == jobs.c.id).where(
                    capacity_waiters.c.approval_id == c.capacity_approval_id,
                    capacity_waiters.c.state == "waiting_capacity", ~self.scope_filter()).limit(1)).first()
            if outside:
                raise ScalerError("finite_cold_waiter_scope_mismatch")
            decision = self.cold.tick(self.leader_id, c.capacity_approval_id)
        else:
            policy = replace(ScalePolicy(**c.scale_policy), approved_remaining_microusd=self.remaining_budget(),
                max_instances=0 if stopping or launch is None else c.scale_policy["max_instances"],
                max_physical_gpus=0 if stopping or launch is None else c.scale_policy["max_physical_gpus"])
            decision = self.scaler.tick(self.leader_id, c.scope, c.pool, demands, slots, policy=policy,
                launch=None if stopping else launch, budget_account_ids=c.budget_account_ids,
                before_create=self.create_allowed)
        return decision

    def tick(self):
        c = self.config
        lease = self.scaler.acquire(c.pool, self.leader_id)
        if lease is None:
            return {"phase": "not_leader", "drained": False}
        self._sync_provider_preparation()
        instances, actions = self._managed()
        stopping = self.stopping()
        if not stopping:
            try:
                verify_policy(c, self.settings)
                with self.repo.engine.connect() as conn:
                    approval = conn.execute(select(capacity_approvals).where(capacity_approvals.c.id == c.capacity_approval_id)).mappings().one()
                if approval["enabled"] != 1 or approval["expires_at"] <= self.repo.clock() or not self.approval_current(approval["payload"]):
                    self.request_drain()
            except Exception:
                self.request_drain()
            stopping = self.stopping()
        if stopping:
            self._drain_intents(instances)
            self._close_unsubmitted()
        # Reconcile old provider operations even after stop/revocation. dry_run
        # is deliberately NOT used as a stop switch: it disables cleanup too.
        decision = self._capacity_decision(instances, actions, stopping)
        self._sync_provider_preparation()
        stopping = self.stopping()
        if not stopping and self._capacity_advance_allowed():
            self.cold.advance_once(c.capacity_approval_id)
        instances, _ = self._managed()
        boot_status = {}
        for row in instances:
            intent = row["id"]
            if row["state"] == "destroyed":
                self.scaler.settle(intent)
                if intent in self.boots:
                    # Idle retirement may become provider-confirmed within
                    # scaler.tick, before boot.tick observes the draining row.
                    # Explicitly wake the existing child's graceful drain path;
                    # otherwise an idle child can wait forever and block the
                    # next on-demand cycle. This never force-kills a process.
                    self.boots[intent].request_drain()
                    self.boots[intent].close_if_safe(destroyed=True)
                continue
            if not row["provider_instance_id"]:
                continue
            if c.provider_preparation_timeout_s is not None:
                with self.repo.engine.connect() as conn:
                    preparation = self.scaler.preparation(conn, row)
                if preparation is None:
                    boot_status[intent] = {"state": "provider_preparation_legacy_adoption_required"}
                    continue
                if preparation["phase"] == "retiring_unused":
                    boot_status[intent] = {"state": preparation["reason"]}
                    continue
                if preparation["phase"] == "awaiting_provider" and not self.scaler.claim_bootstrap(lease, intent):
                    action = self.scaler._action(intent)
                    stage = (action.get("last_observation") or {}).get("preparation_stage")
                    boot_status[intent] = {"state": stage or "provider_preparing"}
                    continue
            execution_allowed = getattr(self.provider, "execution_allowed", None)
            if callable(execution_allowed) and not execution_allowed(intent, row["provider_instance_id"]):
                # A contradicting paid response retains identity/reservation
                # for reconciliation, but must never bootstrap a usable worker.
                boot_status[intent] = {"state": "rental_contract_requires_reconciliation"}
                continue
            if intent not in self.boots:
                from .production_scaler_boot import ProductionBoot
                factory = self.boot_factory or ProductionBoot
                self.boots[intent] = factory(self.repo, self.provider, c, row, self.port_for(intent),
                    config_path=getattr(self, "config_path", None))
                if c.execution_backend == "wangp-worker":
                    configure = getattr(self.boots[intent], "enable_pollable_upload", None)
                    if configure is not None:
                        configure()
            if c.execution_backend == "wangp-worker":
                self.boots[intent].start_guard = lambda intent=intent, lease=lease: self._bootstrap_start_allowed(intent, lease)
            try:
                boot_status[intent] = self.boots[intent].tick(intent, stopping=stopping)
                if boot_status[intent].get("state") in ("qualification_failed", "qualification_deadline_insufficient",
                        "bootstrap_failed", "fleet_recovery_required", "fleet_attention_required",
                        "staging_failed", "staging_cancelled", "staging_recovery_required"):
                    self._boot_failure(row, boot_status[intent])
            except Exception as exc:
                from .lium_provider import LiumError
                boot_status[intent] = {"state": "provider_ttl_unconfirmed" if isinstance(exc, LiumError)
                    and str(exc) == "lium_absolute_ttl_unconfirmed" else "boot_observation_unconfirmed"}
        reason = decision.get("reason")
        if not stopping:
            reason = self._wait_reason(decision, instances, boot_status)
            self._record_wait_reason(reason)
        return self.status(decision=decision.get("state"), reason=reason, boot=boot_status)

    def _wait_reason(self, decision, instances, boot_status):
        reason = decision.get("reason")
        if (any(row["state"] == "creation_unknown" for row in instances)
                or any(v.get("state") == "rental_contract_requires_reconciliation" for v in boot_status.values())):
            reason = "creation_needs_reconciliation"
        elif any(v.get("state") == "configuring_ssh" for v in boot_status.values()):
            reason = "provider_configuring_ssh"
        elif any(v.get("state") == "provider_ttl_unconfirmed" for v in boot_status.values()):
            reason = "provider_ttl_unconfirmed"
        elif any(v.get("state") == "provider_preparing" for v in boot_status.values()):
            reason = "provider_preparing"
        elif any(row["state"] == "starting" for row in instances):
            reason = "gpu_starting"
        elif any(row["state"] in ("ready", "busy") for row in instances):
            reason = "gpu_busy"
        return reason

    def _record_wait_reason(self, reason):
        self.cold.record_wait_reason(self.config.capacity_approval_id, reason or "searching")

    def _project_status(self, value):
        return value

    def status(self, *, fresh_ledger_only=False, **extra):
        rows, _ = self._managed()
        with self.repo.engine.connect() as conn:
            related = select(attempts.c.job_id).join(registered_workers, registered_workers.c.id == attempts.c.worker_id).where(
                registered_workers.c.pool == self.config.pool)
            active = list(conn.execute(select(jobs.c.id).where(
                or_(and_(or_(self.scope_filter(), jobs.c.id.in_(related)),
                    jobs.c.status.not_in(("succeeded", "failed", "cancelled"))),
                    jobs.c.id.in_(self._unsafe_attempts()))).order_by(jobs.c.id).limit(1001)).scalars())
        destroyed = all(r["state"] == "destroyed" for r in rows)
        children_done = fresh_ledger_only or all(b.children_done() for b in self.boots.values())
        drained = self.stopping() and destroyed and not active and children_done
        value = {"cycle_id": self.config.cycle_id, "config_hash": self.config.fingerprint(), "observed_at": self.repo.clock(),
            "phase": "drained" if drained else "draining" if self.stopping() else "running", "drained": drained,
            "ledger_safe": destroyed and not active, "all_destroyed": destroyed,
            "hard_deadline": self.config.hard_deadline, "active_job_ids": active[:1000], "active_jobs_truncated": len(active)>1000,
            "instances": [{k: r[k] for k in ("id", "state", "provider_instance_id", "hard_deadline", "billing_status")} for r in rows],
            "billing_pending": sum(r["billing_status"] != "settled" for r in rows), **extra}
        unknown = [r["id"] for r in rows if r["state"] == "creation_unknown"]
        if unknown:
            value.update(reason="creation_needs_reconciliation",
                recovery={"code": "creation_outcome_unknown", "intent_ids": unknown,
                          "automatic_rerent_allowed": False})
        value = self._project_status(value)
        if fresh_ledger_only:
            value.update(snapshot_only=False, controller_exit_required=True)
        else:
            save(self.config.work_dir/"status.json", value)
        return value


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--enabled", action="store_true")
    parser.add_argument("--credential-stdin", action="store_true")
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--request-drain", action="store_true")
    actions.add_argument("--status", action="store_true")
    actions.add_argument("--slot", help=argparse.SUPPRESS)
    parser.add_argument("--config-hash", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    repo = provider = loader = controller = None
    handlers = {}
    try:
        if args.config is None:
            if args.enabled or args.credential_stdin or args.request_drain or args.status or args.slot:
                raise ScalerError("finite_explicit_config_required")
            print(json.dumps({"phase": "disabled", "provider_calls_enabled": False}))
            return 0
        config = read_config(args.config)
        if not args.enabled and not args.request_drain and not args.status:
            print(json.dumps({"phase": "disabled", "config_valid": True, "config_hash": config.fingerprint(), "provider_calls_enabled": False}))
            return 0
        if args.request_drain:
            if not (config.work_dir/"cycle-state.json").is_file():
                raise ScalerError("finite_cycle_not_started")
            if json.loads((config.work_dir/"cycle-state.json").read_text())["config_hash"] != config.fingerprint():
                raise ScalerError("finite_cycle_configuration_changed")
            (config.work_dir/"drain.flag").touch()
            print(json.dumps({"phase": "drain_requested", "drained": False}))
            return 0
        if args.status:
            receipt = json.loads((config.work_dir/"cycle-state.json").read_text())
            if receipt.get("config_hash") != config.fingerprint():
                raise ScalerError("finite_cycle_configuration_changed")
            settings = Settings.from_environment()
            validate_settings(config, settings, require_policy=False)
            repo = Repository(settings.database_url)
            controller = FiniteController(repo, settings, config, provider=None)
            # Fresh SQL facts, no provider/SSH/registration/DDL. The operator
            # must additionally observe the original container exit code 0.
            print(json.dumps(controller.status(fresh_ledger_only=True)))
            return 0
        if not config.enabled:
            raise ScalerError("finite_config_disabled")
        settings = Settings.from_environment()
        recovering = (config.work_dir/"cycle-state.json").exists()
        validate_settings(config, settings, require_policy=not recovering)
        verify_sources(config)
        if args.slot:
            from .production_scaler_boot import run_child
            return run_child(config, args.slot, args.config_hash, settings)
        verify_identity_files(config)
        loader = stdin_loader(config, sys.stdin.buffer) if args.credential_stdin else AwsLiumLoader(config.secret_arn, config.secret_version_id)
        repo = Repository(settings.database_url)  # Existing schema only.
        provider = LiumProvider(enabled=True, manifests=[LiumManifest(**v) for v in config.manifests], loader=loader,
            idle_probe=lambda tag, instance: controller.idle_probe(tag, instance), clock=repo.clock,
            journal_dir=config.work_dir/"rent-journal")
        config.work_dir.mkdir(parents=True, exist_ok=True)
        with _slot_lock(config.work_dir, "finite-production-scaler") as acquired:
            if not acquired:
                raise ScalerError("finite_controller_already_running")
            controller = FiniteController(repo, settings, config, provider=provider)
            controller.config_path = args.config
            controller.initialize()
            for sig in (signal.SIGINT, signal.SIGTERM):
                handlers[sig] = signal.getsignal(sig)
                signal.signal(sig, lambda *_: controller.request_drain())
            last = None
            while True:
                try:
                    value = controller.tick()
                    compact = {k: value.get(k) for k in ("phase", "drained", "billing_pending")}
                    if compact != last:
                        print(json.dumps(compact), flush=True)
                        last = compact
                    if value.get("drained"):
                        return 0
                except Exception:
                    # Once lifecycle ownership is established, retain tunnels
                    # and keep reconciling. No exception implies safe shutdown.
                    controller.request_drain()
                    print(json.dumps({"phase": "observation_unconfirmed", "drained": False}), flush=True)
                time.sleep(config.interval_s)
    except Exception:
        print(json.dumps({"phase": "finite_configuration_or_recovery_required", "drained": False,
                          "provider_calls_enabled": False}))
        return 1
    finally:
        for sig, handler in handlers.items():
            signal.signal(sig, handler)
        # Normal running return occurs only after all intents are destroyed and
        # attempts terminal. Never close an active boot/fleet on a timeout.
        if provider:
            try:
                provider.close()
            except Exception:
                pass
        if loader:
            try:
                loader.close()
            except Exception:
                pass
        if repo:
            try:
                repo.close()
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
