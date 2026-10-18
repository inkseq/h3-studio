"""Trusted local-file composition for the existing operator rental authority.

Construction reads bounded deployment metadata only. Credentials remain lazy;
the running controller refreshes inventory asynchronously and alone has fenced
rental authority. This file neither initializes budgets nor enables a pool.
"""

from __future__ import annotations

import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import stat
import time
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

from sqlalchemy import insert, select, update

from .lium_provider import LiumError, LiumManifest, LiumProvider
from .targon_provider import TargonError, TargonManifest, TargonProvider
from .operator_capacity import (
    DeploymentBinding,
    OperatorCapacity,
    OperatorError,
    OperatorRegistry,
    require,
    inventory_projection,
    operator_inventory,
    operator_heartbeats,
)
from .operator_controller import OperatorController
from .repository import Repository, Scope, request_hash
from .runtime_catalog import engine_manifest, get_profile, model_for, public_catalog
from .scaler import LaunchSpec

MINIMUM_LIUM_TTL_SECONDS = 3780  # Client whole-hour termination request + submit/queue margins; not a billing minimum.
SOURCE_NAMES = {
    "wangp-bootstrap.py",
    "wangp-manifest.json",
    "wangp-runtime.json",
    "wangp-package.tar.gz",
}


def _absolute(value, *, exists=True, directory=False):
    require(
        isinstance(value, str) and value and not any(c in value for c in "\r\n\x00"),
        "operator_runtime_path_invalid",
        422,
    )
    path = Path(value)
    require(path.is_absolute(), "operator_runtime_absolute_path_required", 422)
    try:
        # Do not silently trust a symlink/junction or a relative parent escape.
        require(".." not in path.parts, "operator_runtime_path_invalid", 422)
        for part in (path, *path.parents):
            require(
                not part.is_symlink()
                and not (hasattr(part, "is_junction") and part.is_junction()),
                "operator_runtime_link_forbidden",
                422,
            )
            if part.exists():
                require(
                    not getattr(part.lstat(), "st_file_attributes", 0)
                    & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400),
                    "operator_runtime_link_forbidden",
                    422,
                )
        if exists:
            require(
                path.is_dir() if directory else path.is_file(),
                "operator_runtime_path_unavailable",
                422,
            )
        return path
    except OSError:
        raise OperatorError("operator_runtime_path_unavailable", 422) from None


def _read(path, maximum, *, protected=False):
    source = _absolute(str(path))
    try:
        with source.open("rb") as handle:
            info = os.fstat(handle.fileno())
            require(
                stat.S_ISREG(info.st_mode)
                and info.st_nlink == 1
                and 0 < info.st_size <= maximum,
                "operator_runtime_file_invalid",
                422,
            )
            if os.name != "nt":
                forbidden = (
                    stat.S_IWGRP | stat.S_IWOTH | (stat.S_IRWXO if protected else 0)
                )
                require(
                    not info.st_mode & forbidden and info.st_uid in (0, os.geteuid()),
                    "operator_runtime_file_permissions",
                    422,
                )
            raw = handle.read(maximum + 1)
        require(len(raw) <= maximum, "operator_runtime_file_too_large", 422)
        return raw
    except OSError:
        raise OperatorError("operator_runtime_file_unavailable", 422) from None


def _decode(raw):
    def unique(pairs):
        value = {}
        for key, item in pairs:
            require(key not in value, "operator_runtime_duplicate_field", 422)
            value[key] = item
        return value

    try:
        value = json.loads(
            raw,
            object_pairs_hook=unique,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()),
        )
        require(isinstance(value, dict), "operator_runtime_schema_invalid", 422)
        return value
    except (ValueError, UnicodeError):
        raise OperatorError("operator_runtime_json_invalid", 422) from None


def load_runtime_config(path, *, validate_private_paths=True):
    source = _absolute(str(path))
    value = _decode(_read(source, 128 * 1024, protected=True))
    required = {
        "schema_version",
        "registry_file",
        "work_dir",
        "ssh_key_file",
        "known_hosts_file",
        "port_start",
    }
    optional = {
        "credential_source",
        "secret_arn",
        "secret_version_id",
        "provider_credentials",
        "cleanup_guard_dir",
        "trust_first_host_key",
        "runtime_python",
    }
    require(
        required <= set(value)
        and not set(value) - required - optional
        and type(value["schema_version"]) is int
        and value["schema_version"] == 1,
        "operator_runtime_schema_invalid",
        422,
    )
    _absolute(value["registry_file"])
    _absolute(value["ssh_key_file"], exists=validate_private_paths)
    _absolute(value["work_dir"], directory=True, exists=validate_private_paths)
    if "cleanup_guard_dir" in value:
        _absolute(
            value["cleanup_guard_dir"], directory=True, exists=validate_private_paths
        )
    trust = value.get("trust_first_host_key", False)
    require(type(trust) is bool, "operator_runtime_trust_invalid", 422)
    hosts = _absolute(
        value["known_hosts_file"], exists=validate_private_paths and not trust
    )
    _absolute(str(hosts.parent), directory=True, exists=validate_private_paths)
    require(
        type(value["port_start"]) is int and 1024 <= value["port_start"] <= 64511,
        "operator_runtime_port_invalid",
        422,
    )
    python = value.get("runtime_python", "/venv/main/bin/python")
    require(
        python == "/venv/main/bin/python", "operator_runtime_python_unqualified", 422
    )
    credential = value.get("credential_source", "central_registry")
    require(
        credential in {"central_registry", "aws_runtime"},
        "operator_runtime_credential_source_invalid",
        422,
    )
    if credential == "central_registry":
        require(
            not {"secret_arn", "secret_version_id", "provider_credentials"}
            & set(value),
            "operator_runtime_credential_conflict",
            422,
        )
    else:
        references = _credential_references(value)
        require(bool(references), "operator_runtime_credential_reference_missing", 422)
        for provider, reference in references.items():
            try:
                _aws_loader(provider, reference)
            except ValueError:
                raise OperatorError(
                    "operator_runtime_credential_reference_invalid", 422
                ) from None
    return {
        **value,
        "config_path": str(source),
        "credential_source": credential,
        "trust_first_host_key": trust,
        "runtime_python": python,
    }


def _credential_references(config):
    references = {}
    legacy = {"secret_arn", "secret_version_id"} & set(config)
    require(
        not legacy or legacy == {"secret_arn", "secret_version_id"},
        "operator_runtime_credential_reference_missing",
        422,
    )
    if legacy:
        references["lium"] = {key: config[key] for key in legacy}
    providers = config.get("provider_credentials", {})
    require(
        isinstance(providers, dict) and not set(providers) - {"targon"},
        "operator_runtime_credential_reference_invalid",
        422,
    )
    for provider, reference in providers.items():
        require(
            isinstance(reference, dict)
            and set(reference) == {"secret_arn", "secret_version_id"},
            "operator_runtime_credential_reference_invalid",
            422,
        )
        references[provider] = reference
    return references


def _aws_loader(provider, reference):
    if provider == "lium":
        from .lium_runtime_aws import AwsLiumLoader

        loader = AwsLiumLoader
    else:
        from .targon_runtime_aws import AwsTargonLoader

        loader = AwsTargonLoader
    return loader(reference["secret_arn"], reference["secret_version_id"])


def _bindings(path):
    value = _decode(_read(path, 1024 * 1024, protected=True))
    require(
        set(value) == {"schema_version", "bindings"}
        and type(value["schema_version"]) is int
        and value["schema_version"] == 1
        and isinstance(value["bindings"], list)
        and len(value["bindings"]) <= 128,
        "operator_registry_schema_invalid",
        422,
    )
    output = []
    for entry in value["bindings"]:
        try:
            item = dict(entry)
            item["launch"] = LaunchSpec(**item["launch"])
            item["scope"] = Scope(**item["scope"])
            for key in ("recipe_ids", "budget_account_ids"):
                require(
                    isinstance(item[key], list),
                    "operator_registry_binding_invalid",
                    422,
                )
                item[key] = tuple(item[key])
            output.append(DeploymentBinding(**item))
        except OperatorError:
            raise
        except (KeyError, TypeError, ValueError):
            raise OperatorError("operator_registry_binding_invalid", 422) from None
    return output


def _validate_sources(binding):
    boot = binding.boot
    require(
        set(boot) == {"provider_manifest_file", "source_dirs", "source_sha256"},
        "operator_runtime_boot_schema_invalid",
        422,
    )
    dirs, hashes = boot["source_dirs"], boot["source_sha256"]
    require(
        isinstance(dirs, list)
        and isinstance(hashes, list)
        and len(dirs) == len(hashes) == binding.execution_slots,
        "operator_runtime_slot_sources_invalid",
        422,
    )
    roots = [_absolute(value, directory=True) for value in dirs]
    require(
        len(
            {
                str(p.resolve()).casefold() if os.name == "nt" else str(p.resolve())
                for p in roots
            }
        )
        == len(roots),
        "operator_runtime_slot_sources_not_unique",
        422,
    )
    try:
        expected = engine_manifest(binding.runtime_profile_id, binding.mode)
    except ValueError:
        raise OperatorError("operator_runtime_profile_invalid", 422) from None
    require(
        expected.digest == binding.engine_manifest_digest,
        "operator_runtime_engine_mismatch",
        422,
    )
    for index, (root, digests) in enumerate(zip(roots, hashes)):
        require(
            isinstance(digests, dict)
            and set(digests) == SOURCE_NAMES
            and all(
                isinstance(v, str) and re.fullmatch(r"[0-9a-f]{64}", v)
                for v in digests.values()
            ),
            "operator_runtime_source_hashes_invalid",
            422,
        )
        content = {}
        for name in sorted(SOURCE_NAMES):
            raw = _read(
                root / name, 16 * 1024 * 1024 if name.endswith(".gz") else 512 * 1024
            )
            require(
                hashlib.sha256(raw).hexdigest() == digests[name],
                "operator_runtime_source_changed",
                422,
            )
            content[name] = raw
        require(
            _decode(content["wangp-manifest.json"]) == expected.document,
            "operator_runtime_engine_mismatch",
            422,
        )
        runtime = _decode(content["wangp-runtime.json"])
        require(
            runtime.get("deployment_profile_id") == binding.runtime_profile_id
            and type(runtime.get("profile_slot_index")) is int
            and runtime["profile_slot_index"] == index
            and type(runtime.get("expected_host_gpus")) is int
            and runtime["expected_host_gpus"] == binding.gpu_count
            and type(runtime.get("port")) is int
            and runtime["port"] == 8199 + index,
            "operator_runtime_slot_identity_mismatch",
            422,
        )
        require(
            runtime.get("source_bundle_sha256") == digests["wangp-package.tar.gz"],
            "operator_runtime_bundle_mismatch",
            422,
        )


def _manifest(binding):
    try:
        value = _decode(
            _read(binding.boot["provider_manifest_file"], 128 * 1024, protected=True)
        )
        manifest = (
            TargonManifest if binding.launch.provider == "targon" else LiumManifest
        )(**value)
        profile = get_profile(binding.runtime_profile_id)
        model = model_for(binding.runtime_profile_id, binding.mode)
    except OperatorError:
        raise
    except (KeyError, TypeError, ValueError, LiumError, TargonError):
        raise OperatorError("operator_runtime_provider_manifest_invalid", 422) from None
    require(
        binding.model_id == profile["model_id"]
        and binding.recipe_ids == (model["generation_recipe_id"],)
        and binding.gpu_type in profile["gpu_models"]
        and binding.gpu_count in profile["gpu_count_options"]
        and binding.execution_slots == binding.gpu_count,
        "operator_runtime_profile_topology_mismatch",
        422,
    )
    if binding.launch.provider == "targon":
        return _targon_manifest(binding, manifest, profile)
    require(
        manifest.server_side_selection
        and not manifest.executor_id
        and not binding.launch.offer_id,
        "operator_runtime_server_selection_required",
        422,
    )
    require(
        manifest.configuration_id == binding.configuration_id
        and manifest.model_id == binding.model_id
        and manifest.template_id == binding.launch.image_id
        and manifest.region == binding.launch.region
        and manifest.gpu_count == binding.gpu_count
        and manifest.execution_slots == binding.execution_slots
        and manifest.compatible_gpu_names == (binding.gpu_type,),
        "operator_runtime_launch_mismatch",
        422,
    )
    require(
        manifest.allow_preflight_only_price_cap
        and binding.expires_at <= manifest.approved_until,
        "operator_runtime_provider_approval_mismatch",
        422,
    )
    require(
        binding.min_ttl_seconds >= MINIMUM_LIUM_TTL_SECONDS
        and binding.max_ttl_seconds <= manifest.termination_hours * 3600 + 60
        and 1 <= manifest.termination_hours <= 4,
        "operator_runtime_provider_ttl_mismatch",
        422,
    )
    require(
        binding.hourly_cost_microusd
        == manifest.max_price_per_gpu_hour_microusd * binding.gpu_count
        and binding.reservation_per_node_microusd
        >= binding.hourly_cost_microusd * manifest.termination_hours
        and binding.reservation_per_node_microusd
        >= math.ceil(binding.hourly_cost_microusd * binding.max_ttl_seconds / 3600),
        "operator_runtime_cost_binding_mismatch",
        422,
    )
    require(
        "min_cpu_cores" not in binding.filters,
        "operator_runtime_cpu_filter_unsupported",
        422,
    )
    expected_filters = {
        "min_ram_gib": manifest.minimum_ram_gib,
        "min_disk_gib": manifest.minimum_disk_gib,
        "max_price_per_gpu_hour_microusd": manifest.max_price_per_gpu_hour_microusd,
        "allowed_countries": list(manifest.allowed_countries),
    }
    if manifest.min_download_mbps is not None:
        require(
            int(manifest.min_download_mbps) == manifest.min_download_mbps,
            "operator_runtime_download_filter_invalid",
            422,
        )
        expected_filters["min_download_mbps"] = int(manifest.min_download_mbps)
    require(
        binding.filters == expected_filters,
        "operator_runtime_hardware_filters_mismatch",
        422,
    )
    require(
        manifest.minimum_ram_gib * 1024**3 >= profile["minimum_ram_bytes"]
        and manifest.minimum_disk_gib * 1024**3 >= profile["minimum_disk_bytes"]
        and manifest.minimum_vram_mib * 1024**2 >= profile["minimum_free_vram_bytes"],
        "operator_runtime_hardware_below_profile",
        422,
    )
    return manifest


def _targon_manifest(binding, manifest, profile):
    require(
        manifest.configuration_id == binding.configuration_id
        and manifest.model_id == binding.model_id
        and manifest.resource_name == binding.launch.offer_id
        and manifest.image_name == binding.launch.image_id
        and not binding.launch.region
        and manifest.gpu_count == binding.gpu_count
        and manifest.execution_slots == binding.execution_slots
        and manifest.gpu_model == "RTX-PRO-6000B"
        and binding.gpu_type
        in {
            "RTX PRO 6000 Blackwell",
            "RTX PRO 6000 Blackwell Server Edition",
            "RTX PRO 6000 Blackwell Workstation Edition",
        },
        "operator_runtime_launch_mismatch",
        422,
    )
    require(
        manifest.allow_preflight_only_price_cap
        and manifest.allow_controller_lifetime
        and binding.expires_at <= manifest.approved_until,
        "operator_runtime_provider_approval_mismatch",
        422,
    )
    require(
        binding.max_ttl_seconds <= manifest.max_lifetime_seconds,
        "operator_runtime_provider_ttl_mismatch",
        422,
    )
    require(
        binding.hourly_cost_microusd == manifest.hourly_cost_cap_microusd
        and manifest.hourly_cost_cap_microusd % binding.gpu_count == 0
        and binding.reservation_per_node_microusd
        >= math.ceil(
            manifest.hourly_cost_cap_microusd * manifest.max_lifetime_seconds / 3600
        ),
        "operator_runtime_cost_binding_mismatch",
        422,
    )
    # Targon exposes neither a qualified network floor nor a country selector.
    # An explicit fixed resource cannot claim to enforce unsupported filters.
    require(
        "min_cpu_cores" not in binding.filters,
        "operator_runtime_cpu_filter_unsupported",
        422,
    )
    require(
        binding.filters
        == {
            "min_ram_gib": manifest.minimum_ram_gib,
            "min_disk_gib": manifest.minimum_disk_gib,
            "allowed_countries": [],
            "max_price_per_gpu_hour_microusd": manifest.hourly_cost_cap_microusd
            // binding.gpu_count,
        },
        "operator_runtime_hardware_filters_mismatch",
        422,
    )
    require(
        manifest.minimum_ram_gib * 1024**3 >= profile["minimum_ram_bytes"]
        and manifest.minimum_disk_gib * 1024**3 >= profile["minimum_disk_bytes"],
        "operator_runtime_hardware_below_profile",
        422,
    )
    return manifest


class _BoundLiumProvider(LiumProvider):
    """Recheck immutable local artifacts before each durable create reservation."""

    def __init__(self, binding, manifest, **kwargs):
        self.binding, self.bound_manifest = binding, manifest
        super().__init__(enabled=True, manifests=(manifest,), **kwargs)

    def validate_launch(self, launch, **kwargs):
        _validate_sources(self.binding)
        require(
            _manifest(self.binding) == self.bound_manifest,
            "operator_runtime_manifest_changed",
        )
        return super().validate_launch(launch, **kwargs)

    def inventory_observation(self):
        result = self.preflight_availability(self.binding.launch)
        cached = self._availability_cache.get(self.bound_manifest)
        require(
            cached is not None
            and cached[1] == result
            and type(cached[0]) in (int, float)
            and math.isfinite(cached[0])
            and 0 <= self.clock() - cached[0] <= 60,
            "operator_inventory_unconfirmed",
        )
        return result, cached[0]


class _BoundTargonProvider(TargonProvider):
    def __init__(self, binding, manifest, **kwargs):
        self.binding, self.bound_manifest = binding, manifest
        super().__init__(enabled=True, manifests=(manifest,), **kwargs)

    def validate_launch(self, launch, **kwargs):
        _validate_sources(self.binding)
        require(
            _manifest(self.binding) == self.bound_manifest,
            "operator_runtime_manifest_changed",
        )
        return super().validate_launch(launch, **kwargs)

    def inventory_observation(self):
        # Base preflight makes one uncached read. Preserve request-start time,
        # so a slow or backwards-clock response never becomes fresh inventory.
        observed_at = self.clock()
        self.preflight_availability(self.binding.launch)
        require(
            type(observed_at) in (int, float)
            and math.isfinite(observed_at)
            and 0 <= self.clock() - observed_at <= 60,
            "operator_inventory_unconfirmed",
        )
        return None, observed_at


def _validated_bindings(config):
    bindings = _bindings(config["registry_file"])
    manifests = {}
    for binding in bindings:
        _validate_sources(binding)
        manifests[binding.binding_id] = _manifest(binding)
        if binding.launch.provider == "targon":
            require(
                "cleanup_guard_dir" in config,
                "operator_runtime_cleanup_guard_required",
                422,
            )
        if config["credential_source"] == "aws_runtime":
            require(
                binding.launch.provider in _credential_references(config),
                "operator_runtime_credential_reference_missing",
                422,
            )
    return bindings, manifests


def _assemble(path, *, clock=time.time, credential_loader=None):
    config = load_runtime_config(path)
    bindings, manifests = _validated_bindings(config)
    loaders = {}
    if credential_loader is None and config["credential_source"] == "aws_runtime":
        loaders = {
            provider: _aws_loader(provider, reference)
            for provider, reference in _credential_references(config).items()
        }
    providers = {}
    for binding in bindings:
        manifest = manifests[binding.binding_id]
        kwargs = {
            "loader": credential_loader or loaders.get(binding.launch.provider),
            "clock": clock,
            "journal_dir": Path(config["work_dir"]) / "rent-journal",
        }
        if binding.launch.provider == "targon":
            from .targon_cleanup import TargonCleanupGuard

            kwargs["cleanup_guard"] = TargonCleanupGuard(
                config["cleanup_guard_dir"], clock=clock
            )
            provider_class = _BoundTargonProvider
        else:
            provider_class = _BoundLiumProvider
        providers[binding.binding_id] = provider_class(binding, manifest, **kwargs)
    registry = OperatorRegistry(
        bindings,
        catalog=public_catalog,
        qualified_providers={"lium"}
        | {binding.launch.provider for binding in bindings},
    )

    def offers(chosen):
        try:
            binding = registry.resolve(chosen)
            require(
                binding.enabled and binding.expires_at > clock(),
                "operator_deployment_not_qualified",
            )
            provider = providers[binding.binding_id]
            if binding.launch.provider == "targon":
                result, observed_at = provider.inventory_observation()
            else:
                result, observed_at = (
                    provider.preflight_availability(binding.launch),
                    clock(),
                )
            reason = (
                None
                if result is None
                else (
                    result
                    if result
                    in {
                        "provider_inventory_unavailable",
                        "provider_inventory_unconfirmed",
                    }
                    else "operator_inventory_unavailable"
                )
            )
            return {
                "status": "available" if result is None else "unavailable",
                "observed_at": observed_at,
                "offers": [],
                "reason_code": reason,
                "minimum_ttl_seconds": binding.min_ttl_seconds,
                "hourly_cost_microusd": binding.hourly_cost_microusd,
                "hourly_cost_basis": "approved_ceiling",
            }
        except Exception:
            return {
                "status": "unavailable",
                "observed_at": clock(),
                "offers": [],
                "reason_code": "operator_inventory_unavailable",
            }

    registry.offers_reader = offers
    return config, registry, providers


def create_registry(path, *, repository=None):
    """API/worker: local public metadata and database only, never a provider.

    The API needs no SSH private key, writable controller directory, AWS role,
    provider credential, or outbound network. Source/config paths remain identical
    across processes and their small immutable metadata must be mounted.
    """
    config = load_runtime_config(path, validate_private_paths=False)
    bindings, _ = _validated_bindings(config)
    registry = OperatorRegistry(
        bindings,
        catalog=public_catalog,
        inventory_required=True,
        qualified_providers={"lium"}
        | {binding.launch.provider for binding in bindings},
    )

    def offers(chosen):
        binding = registry.resolve(chosen)
        if repository is None:
            return {
                "status": "unavailable",
                "observed_at": None,
                "stale": True,
                "offers": [],
                "reason_code": "operator_inventory_not_configured",
            }
        with repository.engine.connect() as connection:
            return inventory_projection(connection, binding, repository.clock())

    registry.offers_reader = offers
    return registry


class LiumMarketRefresh:
    """Provider-wide stock cache using the controller's existing identity.

    Keep this full GET separate from per-binding admission probes, which may be
    filtered or dry-run selectors. The transport bounds live in scan_lium; no
    Future timeout substitutes for them. A slow read never holds the tick loop.
    """

    def __init__(self, repo, loader):
        self.repo, self.loader = repo, loader
        self.executor = None
        self.pending = None
        self.last = float("-inf")
        self.closed = False

    def _probe(self):
        from .capacity_inventory import scan_lium

        return scan_lium(self.loader, clock=self.repo.clock)

    def __call__(self, *, stopping=False):
        if stopping:
            self.closed = True
            if self.executor:
                # Cancel a queued read, but do not wait on an in-flight bounded
                # HTTP request while leases or output collection need ticks.
                self.executor.shutdown(wait=False, cancel_futures=True)
                self.executor = None
            return
        if self.closed:
            return
        if self.pending:
            if not self.pending.done():
                return
            from .capacity_market import publish_observation

            try:
                observation = self.pending.result()
                publish_observation(self.repo, observation)
            except Exception:
                publish_observation(
                    self.repo,
                    {
                        "provider": "lium",
                        "status": "error",
                        "observed_at": self.repo.clock(),
                        "offers": [],
                    },
                )
            finally:
                self.pending = None
        now = self.repo.clock()
        if now - self.last < 30:
            return
        if self.executor is None:
            self.executor = ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="lium-market"
            )
        self.last = now
        self.pending = self.executor.submit(self._probe)


class InventoryRefresh:
    """One bounded read-only probe off the lifecycle loop; no raw response saved.

    Slow inventory must not delay provider-lifetime refresh, collection or drain.
    No thread has authority to create a rental. Publication occurs on the main
    loop only while this process still owns the current heartbeat projection.
    """

    def __init__(self, repo, registry, providers, *, market_refresh=None):
        self.repo, self.registry, self.providers = repo, registry, providers
        self.market_refresh = market_refresh
        self.executor = None
        self.pending = None
        self.last = {}

    def _probe(self, binding):
        try:
            result, observed_at = self.providers[
                binding.binding_id
            ].inventory_observation()
            reason = (
                None
                if result is None
                else result
                if result
                in {"provider_inventory_unavailable", "provider_inventory_unconfirmed"}
                else "operator_inventory_unavailable"
            )
            return "available" if result is None else "unavailable", reason, observed_at
        except Exception:
            return "unavailable", "operator_inventory_unavailable", self.repo.clock()

    def __call__(self, controller_id, *, stopping=False):
        if self.market_refresh is not None:
            try:
                self.market_refresh(stopping=stopping)
            except Exception:
                pass  # A failed stock write must not block rental reconciliation.
        if self.pending and self.pending[1].done():
            binding, future = self.pending
            status, reason, observed_at = future.result()
            self.pending = None
            if not stopping:
                with self.repo.transaction() as connection:
                    self.repo._lock_capacity(connection)
                    heartbeat = (
                        connection.execute(
                            select(operator_heartbeats).where(
                                operator_heartbeats.c.id == "global"
                            )
                        )
                        .mappings()
                        .first()
                    )
                    if heartbeat and heartbeat["controller_id"] == controller_id:
                        value = dict(
                            binding_hash=binding.fingerprint,
                            controller_id=controller_id,
                            observed_at=observed_at,
                            status=status,
                            reason_code=reason,
                        )
                        row = connection.execute(
                            select(operator_inventory.c.binding_id).where(
                                operator_inventory.c.binding_id == binding.binding_id
                            )
                        ).first()
                        if row:
                            connection.execute(
                                update(operator_inventory)
                                .where(
                                    operator_inventory.c.binding_id
                                    == binding.binding_id
                                )
                                .values(**value)
                            )
                        else:
                            connection.execute(
                                insert(operator_inventory).values(
                                    binding_id=binding.binding_id, **value
                                )
                            )
        if stopping:
            if self.executor:
                self.executor.shutdown(wait=False, cancel_futures=True)
            return
        if self.pending:
            return
        now = self.repo.clock()
        eligible = [
            b
            for b in self.registry.bindings.values()
            if b.enabled
            and now < b.expires_at
            and now - self.last.get(b.binding_id, float("-inf")) >= 30
        ]
        if not eligible:
            return
        binding = min(
            eligible, key=lambda b: self.last.get(b.binding_id, float("-inf"))
        )
        if self.executor is None:
            self.executor = ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="operator-inventory"
            )
        self.last[binding.binding_id] = now
        self.pending = (binding, self.executor.submit(self._probe, binding))


def _status_writer(config, *, clock):
    # Bind the raw approved JSON, not defaults added by the loader.
    digest = request_hash(
        _decode(_read(config["config_path"], 128 * 1024, protected=True))
    )
    path = Path(config["work_dir"]) / "controller-status.json"

    def write(controller_id, value):
        state = value["state"]
        result = {
            "schema_version": 1,
            "runtime_config_sha256": digest,
            "controller_id": controller_id,
            "observed_at": clock(),
            "state": state,
            "local_connections_released": state == "shutdown_complete"
            and value.get("local_connections_released") is True,
            "cloud_removal_confirmed": False,
            "billing_settled": False,
        }
        require(
            state
            in {
                "running",
                "degraded",
                "draining",
                "shutdown_waiting",
                "shutdown_complete",
            },
            "operator_status_state_invalid",
        )
        _absolute(str(path), exists=False)
        temporary = path.with_name(".controller-status-" + uuid.uuid4().hex + ".tmp")
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(result, stream, sort_keys=True, separators=(",", ":"))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        except Exception:
            # Keep a failed temporary file for operator inspection; do not
            # expose arbitrary filesystem/credential context through errors.
            raise OperatorError("operator_status_write_failed") from None

    return write


def create_controller(
    path, *, repository=None, settings=None, boot_factory=None, credential_loader=None
):
    """CLI factory. Existing schema, policies, budgets and pools remain unchanged."""
    if settings is None:
        from .settings import Settings

        settings = Settings.from_environment()
    repo = repository if repository is not None else Repository(settings.database_url)
    config, registry, providers = _assemble(
        path, clock=repo.clock, credential_loader=credential_loader
    )
    service = OperatorCapacity(repo, settings, registry)
    if boot_factory is None:

        def boot_factory(*args):
            from .operator_boot import create_boot

            return create_boot(*args)

    def boot(binding, intent, chosen):
        return boot_factory(
            repo, providers[binding.binding_id], binding, intent, chosen, config
        )

    # All Lium bindings use the same explicit service/profile and validated
    # loader. Do not copy credentials or give provider egress to the API.
    lium = next(
        (
            provider
            for provider in providers.values()
            if isinstance(provider, _BoundLiumProvider)
        ),
        None,
    )
    from .capacity_scan import ProviderMarketRefresh

    market = ProviderMarketRefresh(repo, lium._loader if lium is not None else None)
    return OperatorController(
        service,
        provider_factory=lambda binding: providers[binding.binding_id],
        boot_factory=boot,
        enabled=True,
        inventory_refresh=InventoryRefresh(
            repo, registry, providers, market_refresh=market
        ),
        status_writer=_status_writer(config, clock=repo.clock),
    )


def create_controller_from_stdin(path, *, stream=None, **kwargs):
    """Reuse the reviewed host-to-container memory envelope, without AWS access."""
    config = load_runtime_config(path)
    require(
        config["credential_source"] == "aws_runtime",
        "operator_stdin_requires_aws_identity",
        422,
    )
    require("credential_loader" not in kwargs, "operator_stdin_loader_conflict", 422)
    from .production_scaler import stdin_loader

    try:
        source = stream if stream is not None else sys.stdin.buffer
        raw = source.read(49153)
        require(len(raw) <= 49152, "operator_credential_envelope_invalid", 422)
        envelope = _decode(raw)
        references = _credential_references(config)
        if "schema_version" in envelope:
            require(
                set(envelope) == {"schema_version", "credentials"}
                and type(envelope["schema_version"]) is int
                and envelope["schema_version"] == 2
                and isinstance(envelope["credentials"], dict)
                and set(envelope["credentials"]) == set(references),
                "operator_credential_envelope_invalid",
                422,
            )
            credentials = envelope["credentials"]
        else:
            require(
                set(references) == {"lium"}, "operator_credential_envelope_invalid", 422
            )
            credentials = {"lium": envelope}
        loaders = {}
        for provider, reference in references.items():
            memory = io.BytesIO(json.dumps(credentials[provider]).encode("utf-8"))
            if provider == "lium":
                loaders[provider] = stdin_loader(SimpleNamespace(**reference), memory)
            else:
                from .targon_runtime_aws import stdin_targon_loader

                loaders[provider] = stdin_targon_loader(
                    reference["secret_arn"], reference["secret_version_id"], memory
                )

        def loader(service, *, profile):
            require(
                service in loaders, "operator_runtime_credential_provider_mismatch", 422
            )
            return loaders[service](service, profile=profile)
    except Exception:
        raise OperatorError("operator_credential_envelope_invalid", 422) from None
    return create_controller(path, credential_loader=loader, **kwargs)
