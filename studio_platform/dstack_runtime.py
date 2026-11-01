"""CPU native runtime bridge for positively owned, single-GPU dstack runs.

The original ledger journals setup once. Local files are protected transport
receipts/credentials, never a second business ledger. Reconnect observes the
same remote marker/incarnation; it cannot reinstall, restart or mint a token.
An original journal known not to have dispatched setup can resume its immutable
source upload, then make the single initial launch.
Provider endpoint extraction and worker lifecycle stay with the controller.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field, replace
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
from types import MappingProxyType
from typing import Protocol
from uuid import UUID, uuid4

from .control import WorkerSpec
from .dstack_capacity import DstackError
from .operator_capacity import OperatorError
from .fleet import SlotConfig
from .inference.wangp_contract import HostReadiness, safe_model_progress
from .inference.wangp_factory import create_backend, read_document
from .inference.wangp_http import HTTPWanGPTransport
from .runtime_catalog import engine_manifest, get_profile, profile_output_delivery
from .runtime_hosts.wangp_http import private_token_file
from .wangp_bootstrap import (
    SOURCE_NAMES,
    BootError,
    WanGPSSHHost,
    _write_immutable,
    read_sources,
    validate_report,
    safe_preparation_report,
)
from .slot_lock import slot_lock
from .providers.registry import BACKEND_PROVIDERS

GPU_UUID = re.compile(r"GPU-[a-fA-F0-9]{8}(?:-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12}\Z")
PROVIDERS = BACKEND_PROVIDERS
IDENTITY_FIELDS = (
    "intent_id",
    "run_id",
    "provider_instance_id",
    "backend",
    "profile_id",
    "mode",
    "model_id",
    "configuration_id",
    "manifest_digest",
    "spec_digest",
)


def _require(condition, code):
    if not condition:
        raise DstackError(code)


def _uuid(value):
    try:
        _require(
            isinstance(value, str) and str(UUID(value)) == value,
            "dstack_runtime_identity_invalid",
        )
    except (ValueError, TypeError, AttributeError):
        raise DstackError("dstack_runtime_identity_invalid") from None
    return value


def _absolute(value):
    path = Path(value)
    _require(
        path.is_absolute() and ".." not in path.parts, "dstack_runtime_path_invalid"
    )
    for part in (path, *path.parents):
        _require(
            not part.is_symlink()
            and not (hasattr(part, "is_junction") and part.is_junction()),
            "dstack_runtime_link_forbidden",
        )
    return path


@dataclass(frozen=True)
class DstackRuntimeConfig:
    """Trusted deployment selection, not user-controlled job parameters."""

    work_dir: Path
    source_dir: Path
    ssh_key_file: Path
    known_hosts_file: Path
    local_port: int
    pool: str
    provider: str
    deployment_profile_id: str
    mode: str
    model_id: str
    configuration_id: str
    engine_manifest_digest: str
    source_sha256: dict = field(repr=False)
    min_gpu_bytes: int = 30 * 1024**3
    trust_first_host_key: bool = False
    runtime_python: str = "/venv/main/bin/python"
    # Dial-out workers (#859): CPU port of the worker's boot exec endpoint.
    status_port: int | None = None
    profile_slot_index: int = field(default=0, init=False)
    expected_host_gpus: int = field(default=1, init=False)
    execution_backend: str = field(default="wangp-worker", init=False)
    output_delivery: str = field(default="native-frames-v1", init=False)
    recipe_ids: tuple[str, ...] = field(default=(), init=False)

    def __post_init__(self):
        for name in ("work_dir", "source_dir", "ssh_key_file", "known_hosts_file"):
            object.__setattr__(self, name, _absolute(getattr(self, name)))
        _require(
            all(
                type(port) is int and 1024 <= port <= 65535
                for port in (self.local_port, self.status_port or self.local_port)
            )
            and self.status_port != self.local_port,
            "dstack_runtime_port_invalid",
        )
        _require(
            self.provider in PROVIDERS.values()
            and self.mode in {"fl", "ref"}
            and type(self.trust_first_host_key) is bool
            and self.runtime_python == "/venv/main/bin/python",
            "dstack_runtime_configuration_invalid",
        )
        _require(
            all(
                isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", value)
                for value in (self.pool, self.configuration_id)
            ),
            "dstack_runtime_configuration_invalid",
        )
        try:
            manifest = engine_manifest(self.deployment_profile_id, self.mode)
            profile = get_profile(self.deployment_profile_id)
        except ValueError:
            raise DstackError("dstack_runtime_profile_invalid") from None
        floor = profile.get("hardware_admission", {}).get(
            "minimum_total_vram_bytes",
            (30 if "Pruned" in profile["model_id"] else 90) * 1024**3,
        )
        _require(
            self.model_id == profile["model_id"]
            and self.engine_manifest_digest == manifest.digest
            and type(self.min_gpu_bytes) is int
            and self.min_gpu_bytes >= floor,
            "dstack_runtime_manifest_binding_invalid",
        )
        object.__setattr__(
            self, "recipe_ids", (manifest.document["generation_recipe_id"],)
        )
        object.__setattr__(
            self, "output_delivery", profile_output_delivery(self.deployment_profile_id)
        )
        _require(
            type(self.source_sha256) is dict
            and set(self.source_sha256) == SOURCE_NAMES
            and all(
                isinstance(v, str) and re.fullmatch(r"[0-9a-f]{64}", v)
                for v in self.source_sha256.values()
            ),
            "dstack_runtime_source_hashes_invalid",
        )
        object.__setattr__(
            self, "source_sha256", MappingProxyType(dict(self.source_sha256))
        )


class RuntimeStore(Protocol):
    def load(self, intent_id: str) -> dict: ...
    def begin_bootstrap(
        self, intent_id: str, run_id: str, instance_id: str
    ) -> bool: ...


class RuntimeHost(Protocol):
    """Existing verified SSH transport; no provider credential methods."""

    def ensure_connected(self) -> None: ...
    def upload(self, files: dict) -> None: ...
    def start(self, identity: dict) -> dict: ...
    def report(self) -> dict: ...
    def open_tunnel(self, port: int) -> None: ...
    def close(self) -> None: ...


def _step(code, call, *args):
    """Name the readiness step that failed (#193); the old catch-all hid it."""
    try:
        return call(*args)
    except (DstackError, OperatorError):
        raise
    except Exception:
        raise DstackError(code) from None


class DstackSSHHost(WanGPSSHHost):
    def _capture_system_observation(self):
        # The legacy inventory hook validates Lium/Targon instance-ID formats.
        # dstack's prepared digest image is checked by the original native
        # profile bootstrap; never pretend a Vast ID is a legacy allocation.
        return None


class DstackNativeRuntime:
    """readiness callback + exact SlotConfig for the parent CPU controller.

    config_for_binding(binding) chooses immutable prepared sources/settings.
    coordinates_for_run(binding, run) extracts trusted dstack SSH coordinates;
    the controller owns that pinned API contract. Only direct root SSH is
    supported; proxy/dockerized topology must be rejected by the extractor.
    This bridge does not discover, rent, stop, register or execute business jobs.
    """

    def __init__(
        self,
        store: RuntimeStore,
        config_for_binding,
        coordinates_for_run,
        *,
        ssh_factory=DstackSSHHost,
        transport_factory=HTTPWanGPTransport,
        backend_factory=create_backend,
        max_hosts=128,
        grant_writer=None,
        dialout_factory=None,
    ):
        _require(
            type(max_hosts) is int and 1 <= max_hosts <= 128,
            "dstack_runtime_host_limit_invalid",
        )
        self.store, self.config_for_binding, self.coordinates_for_run = (
            store,
            config_for_binding,
            coordinates_for_run,
        )
        self.ssh_factory, self.transport_factory, self.backend_factory = (
            ssh_factory,
            transport_factory,
            backend_factory,
        )
        self.max_hosts, self._hosts, self._slots = max_hosts, {}, {}
        # Dial-out (#859): prepares a worker's one-time grant; None disables it.
        self.grant_writer, self.dialout_factory = grant_writer, dialout_factory

    def _dialout_host(self, binding, config, identity, files, directory):
        """Write the grant once, then reach the worker through its tunnel."""
        intent_id = binding["intent_id"]
        _require(
            self.grant_writer is not None and config.status_port is not None,
            "dstack_runtime_dialout_unconfigured",
        )
        self.grant_writer(binding, config, identity, directory)
        coordinates = {
            "host": "127.0.0.1",
            "port": config.status_port,
            "username": "root",
            "instance_id": binding["provider_instance_id"],
            "slot_key": intent_id,
        }
        if intent_id not in self._hosts:
            _require(
                len(self._hosts) < self.max_hosts, "dstack_runtime_host_limit_reached"
            )
            try:
                host = self.dialout_factory(config, coordinates)
            except Exception:
                # The worker has not exchanged its grant or dialed out yet.
                raise DstackError(
                    "dstack_runtime_ssh_unavailable_or_host_key_untrusted"
                ) from None
            self._hosts[intent_id] = (identity, coordinates, host)
        cached_identity, cached_coordinates, host = self._hosts[intent_id]
        _require(
            cached_identity == identity and cached_coordinates == coordinates,
            "dstack_runtime_host_identity_changed",
        )
        try:
            host.ensure_connected()
            host.upload(files)
            host.start(identity)
        except BootError as error:
            raise DstackError(
                "dstack_runtime_upload_reconciliation_required"
                if error.args == ("bootstrap_existing_source_mismatch",)
                else "dstack_runtime_ssh_unavailable_or_host_key_untrusted"
            ) from None
        return host

    def _binding(self, binding, run, config):
        _require(
            type(binding) is dict and isinstance(config, DstackRuntimeConfig),
            "dstack_runtime_binding_invalid",
        )
        _uuid(binding.get("intent_id"))
        _uuid(binding.get("run_id"))
        current = self.store.load(binding["intent_id"])
        _require(
            type(current) is dict
            and all(current.get(k) == binding.get(k) for k in IDENTITY_FIELDS)
            and current.get("run_spec") == binding.get("run_spec")
            and current.get("apply_started") is True
            and current.get("provider_instance_id"),
            "dstack_runtime_binding_changed",
        )
        _require(
            type(run) is dict
            and run.get("id") == binding["run_id"]
            and run.get("status") == "running",
            "dstack_runtime_run_not_running",
        )
        provisioning = (run.get("latest_job_submission") or {}).get(
            "job_provisioning_data"
        ) or {}
        gpus = provisioning.get("instance_type", {}).get("resources", {}).get("gpus")
        _require(
            provisioning.get("instance_id") == binding["provider_instance_id"]
            and provisioning.get("backend") == binding["backend"]
            and isinstance(gpus, list)
            and len(gpus) == 1,
            "dstack_runtime_allocation_mismatch",
        )
        _require(
            PROVIDERS.get(binding["backend"]) == config.provider
            and (
                binding["profile_id"],
                binding["mode"],
                binding["model_id"],
                binding["configuration_id"],
                binding["manifest_digest"],
            )
            == (
                config.deployment_profile_id,
                config.mode,
                config.model_id,
                config.configuration_id,
                config.engine_manifest_digest,
            ),
            "dstack_runtime_configuration_mismatch",
        )
        image = binding["run_spec"]["configuration"]["image"]
        _require(
            re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9./:_-]*@sha256:[0-9a-f]{64}", image),
            "dstack_runtime_image_unpinned",
        )
        return current, image

    def _sources(self, config):
        files, manifest = read_sources(config)
        hashes = {
            name: hashlib.sha256(data).hexdigest() for name, data in files.items()
        }
        _require(hashes == dict(config.source_sha256), "dstack_runtime_source_changed")
        runtime = json.loads(files["wangp-runtime.json"])
        _require(
            runtime.get("source_bundle_sha256") == hashes["wangp-package.tar.gz"]
            and runtime.get("status_path")
            == "/workspace/h3-studio/profile-slot-0/setup-status.json"
            and runtime.get("prepared_root") == "/opt/workspace-internal/Wan2GP"
            and not runtime.get("dependency_artifact_url")
            and not runtime.get("dependency_artifact_path"),
            "dstack_runtime_prepared_source_required",
        )
        return files, manifest, hashes

    def _coordinates(self, binding, run):
        coordinates = self.coordinates_for_run(dict(binding), run)
        _require(
            type(coordinates) is dict
            and set(coordinates) - {"jump"}
            == {"host", "port", "username", "instance_id"},
            "dstack_runtime_ssh_coordinates_invalid",
        )
        jump = coordinates.get("jump")
        _require(
            jump is None
            or type(jump) is dict
            and set(jump) == {"host", "port", "username"}
            and isinstance(jump["host"], str)
            and re.fullmatch(r"[A-Za-z0-9.:_-]{1,253}", jump["host"])
            and type(jump["port"]) is int
            and 1 <= jump["port"] <= 65535
            and isinstance(jump["username"], str)
            and re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", jump["username"]),
            "dstack_runtime_ssh_coordinates_invalid",
        )
        _require(
            coordinates.get("host") is not None and coordinates.get("port") is not None,
            "dstack_runtime_ssh_coordinates_unavailable",
        )
        _require(
            isinstance(coordinates["host"], str)
            and re.fullmatch(r"[A-Za-z0-9.:_-]{1,253}", coordinates["host"])
            and type(coordinates["port"]) is int
            and 1 <= coordinates["port"] <= 65535
            and coordinates["username"] == "root"
            and coordinates["instance_id"] == binding["provider_instance_id"],
            "dstack_runtime_ssh_coordinates_invalid",
        )
        return copy.deepcopy(coordinates)

    @staticmethod
    def _save(path, state):
        _absolute(path)
        temporary = path.with_name(path.name + "." + uuid4().hex + ".tmp")
        try:
            with os.fdopen(
                os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600),
                "w",
                encoding="utf-8",
            ) as out:
                json.dump(state, out, sort_keys=True)
                out.flush()
                os.fsync(out.fileno())
            temporary.replace(path)
            DstackNativeRuntime._sync_directory(path.parent)
        finally:
            if temporary.exists():
                temporary.unlink()

    @staticmethod
    def _sync_directory(directory):
        if os.name == "posix":
            descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)

    @staticmethod
    def _recover_token_alias(token_path):
        # A hard interruption between link and unlink can retain our staging
        # alias. Remove only one exact owned inode in this locked intent dir;
        # unknown hard links still fail checked_reader, never mint a new token.
        info = token_path.lstat()
        if info.st_nlink != 2 or not stat.S_ISREG(info.st_mode):
            return
        aliases = []
        for index, candidate in enumerate(token_path.parent.glob("wangp-token.*.tmp")):
            if index >= 32:
                return
            if not re.fullmatch(r"wangp-token\.[0-9a-f]{32}\.tmp", candidate.name):
                continue
            current = candidate.lstat()
            if (
                (current.st_dev, current.st_ino) == (info.st_dev, info.st_ino)
                and stat.S_ISREG(current.st_mode)
                and current.st_uid == info.st_uid
                and (
                    os.name != "posix"
                    or current.st_uid == os.getuid()
                    and not current.st_mode & 0o077
                )
            ):
                aliases.append(candidate)
            if len(aliases) > 1:
                return
        if len(aliases) == 1:
            aliases[0].unlink()
            DstackNativeRuntime._sync_directory(token_path.parent)

    def readiness(self, binding, run):
        """Explicit controller callback; import/construction has no SSH work."""
        intent_id = _uuid(binding.get("intent_id") if type(binding) is dict else None)
        self._slots.pop(intent_id, None)
        try:
            config = self.config_for_binding(dict(binding))
            current, image = self._binding(binding, run, config)
            files, manifest, hashes = self._sources(config)
            config.work_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            with slot_lock(config.work_dir, "dstack-bootstrap-" + intent_id) as locked:
                _require(locked, "dstack_runtime_observation_busy")
                directory = _absolute(config.work_dir / intent_id)
                created = not directory.exists()
                directory.mkdir(mode=0o700, exist_ok=True)
                if created:
                    self._sync_directory(config.work_dir)
                receipt = directory / "bootstrap-state.json"
                identity = {
                    "intent_id": intent_id,
                    "instance_id": binding["provider_instance_id"],
                    "configuration_id": config.configuration_id,
                    "provider": config.provider,
                    "backend": "wangp-worker",
                    "engine_manifest_digest": config.engine_manifest_digest,
                    "output_delivery": config.output_delivery,
                    "deployment_profile_id": config.deployment_profile_id,
                    "runtime_python": config.runtime_python,
                    "profile_slot_index": 0,
                    "expected_host_gpus": 1,
                    "sources": hashes,
                    "dstack_run_id": binding["run_id"],
                    "dstack_image": image,
                }
                state = (
                    read_document(receipt, maximum=16384) if receipt.exists() else None
                )
                if state is not None:
                    _require(
                        state.get("identity") == identity
                        and state.get("local_port") == config.local_port,
                        "dstack_runtime_receipt_changed",
                    )
                    _require(
                        state.get("phase")
                        in {"journaled", "start_unknown", "booting", "runtime_ready"},
                        "dstack_runtime_receipt_phase_invalid",
                    )
                    if (
                        state["phase"] == "start_unknown"
                        and current.get("bootstrap_launch_started") is False
                    ):
                        # The durable launch journal proves no remote call was
                        # permitted yet (e.g. DB failed before launch CAS).
                        # Absent/historical or true journals remain unknown.
                        state["phase"] = "journaled"
                        self._save(receipt, state)
                _require(
                    current.get("bootstrap_started") is True
                    or state is None
                    or state.get("phase") == "journaled",
                    "dstack_runtime_existing_receipt_requires_reconciliation",
                )
                # dstack RUNNING can precede SSH metadata/availability. Only
                # read-only connection work belongs before the nonreplayable
                # journal; do not consume the initial bootstrap while waiting
                # for an endpoint or its verified host key.
                # Dial-out workers (#859) connect only after their grant exists.
                dialout = bool(binding.get("boot"))
                if not dialout:
                    coordinates = self._coordinates(binding, run)
                    if intent_id not in self._hosts:
                        _require(
                            len(self._hosts) < self.max_hosts,
                            "dstack_runtime_host_limit_reached",
                        )
                        try:
                            host = self.ssh_factory(config, coordinates)
                        except Exception:
                            raise DstackError(
                                "dstack_runtime_ssh_unavailable_or_host_key_untrusted"
                            ) from None
                        self._hosts[intent_id] = (identity, coordinates, host)
                    cached_identity, cached_coordinates, host = self._hosts[intent_id]
                    _require(
                        cached_identity == identity
                        and cached_coordinates == coordinates,
                        "dstack_runtime_host_identity_changed",
                    )
                    try:
                        host.ensure_connected()
                    except Exception:
                        raise DstackError(
                            "dstack_runtime_ssh_unavailable_or_host_key_untrusted"
                        ) from None
                if state is None and not current.get("bootstrap_started"):
                    # Stage the original private runtime credential once on
                    # CPU. WanGPSSHHost.start uploads these same bytes; it must
                    # never mint a replacement during routine reconnect.
                    token_path = directory / "wangp-token"
                    if token_path.exists():
                        self._recover_token_alias(token_path)
                        private_token_file(
                            token_path
                        )  # Crash during local staging; no remote launch yet.
                    else:
                        temporary = directory / ("wangp-token." + uuid4().hex + ".tmp")
                        try:
                            with os.fdopen(
                                os.open(
                                    temporary,
                                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                                    0o600,
                                ),
                                "w",
                            ) as out:
                                out.write(secrets.token_urlsafe(48))
                                out.flush()
                                os.fsync(out.fileno())
                            os.link(
                                temporary, token_path
                            )  # Atomic no-overwrite install of complete bytes.
                        finally:
                            if temporary.exists():
                                temporary.unlink()
                        self._sync_directory(directory)
                    state = {
                        "identity": identity,
                        "phase": "journaled",
                        "local_port": config.local_port,
                    }
                    self._save(receipt, state)
                # Material is staged before the database journal. A CPU disk
                # failure cannot consume a one-shot remote launch. Resuming
                # staged material always retains its original token/identity.
                self.store.begin_bootstrap(
                    intent_id, binding["run_id"], binding["provider_instance_id"]
                )
                if state is not None:
                    _require(
                        state is not None and (directory / "wangp-token").is_file(),
                        "dstack_runtime_recovery_material_missing",
                    )
                    private_token_file(
                        directory / "wangp-token"
                    )  # Verify, never recreate.
                else:
                    raise DstackError("dstack_runtime_recovery_material_missing")
                if state["phase"] == "journaled" and dialout:
                    self.store.authorize_bootstrap_launch(
                        intent_id, binding["run_id"], binding["provider_instance_id"]
                    )
                    state["phase"] = "start_unknown"
                    self._save(receipt, state)  # Before the grant enables launch.
                    _require(
                        self.store.begin_runtime_launch(
                            intent_id,
                            binding["run_id"],
                            binding["provider_instance_id"],
                        ),
                        "dstack_runtime_launch_requires_reconciliation",
                    )
                    state["phase"] = "booting"
                    self._save(receipt, state)
                elif state["phase"] == "journaled":
                    self.store.authorize_bootstrap_launch(
                        intent_id, binding["run_id"], binding["provider_instance_id"]
                    )
                    # Source upload is immutable and has no setup launch. A
                    # failed upload can retry the same bytes/token; an existing
                    # partial file instead requires reconciliation below. No
                    # phase after start_unknown can replay upload or setup.
                    try:
                        host.upload(files)
                    except BootError as error:
                        if error.args == ("bootstrap_existing_source_mismatch",):
                            # A partial file is not silently overwritten by
                            # the original transport. Preserve the journal,
                            # explain the reconciliation hold, never launch.
                            raise DstackError(
                                "dstack_runtime_upload_reconciliation_required"
                            ) from None
                        raise DstackError("dstack_runtime_upload_unconfirmed") from None
                    except Exception:
                        raise DstackError("dstack_runtime_upload_unconfirmed") from None
                    state["phase"] = "start_unknown"
                    self._save(receipt, state)  # Before the non-replayable launch.
                    _require(
                        self.store.begin_runtime_launch(
                            intent_id,
                            binding["run_id"],
                            binding["provider_instance_id"],
                        ),
                        "dstack_runtime_launch_requires_reconciliation",
                    )
                    try:
                        host.start(identity)
                    except Exception:
                        raise DstackError(
                            "dstack_runtime_bootstrap_response_unknown"
                        ) from None
                    state["phase"] = "booting"
                    self._save(receipt, state)
                if dialout:
                    host = self._dialout_host(
                        binding, config, identity, files, directory
                    )
                report = host.report()
                _require(
                    type(report) is dict and report.get("identity") == identity,
                    "dstack_runtime_remote_identity_unconfirmed",
                )
                preparation = safe_preparation_report(report)
                # The SSH report has already discarded its raw receipt fields.
                # Accept only the same numeric allowlist from that protected seam.
                if type(report.get("preparation")) is dict:
                    supplied = report["preparation"]
                    preparation = safe_preparation_report(
                        {
                            "phase": supplied.get("phase"),
                            "started_unix": supplied.get("started_at"),
                            "phase_started_unix": supplied.get("phase_started_at"),
                            "updated_unix": supplied.get("updated_at"),
                            **(
                                supplied.get("download")
                                if type(supplied.get("download")) is dict
                                else {}
                            ),
                            "state": report.get("state"),
                            "error_code": report.get("error_code"),
                            "failure_phase": report.get("failure_phase"),
                        }
                    )

                def progress_error(code):
                    error = DstackError(code)
                    error.preparation = preparation
                    return error

                if report.get("state") == "failed":
                    raise progress_error("dstack_runtime_bootstrap_failed")
                if (
                    report.get("state") in {"unknown", "reconcile_required"}
                    or report.get("phase") == "runtime_start_unknown"
                ):
                    raise progress_error(
                        "dstack_runtime_bootstrap_reconciliation_required"
                    )
                if report.get("state") != "ready":
                    # Only these fixed progress codes leave the private host;
                    # preserve real download/start progress without exposing
                    # report text or pretending process startup loaded weights.
                    phases = {
                        "model_download": "dstack_runtime_downloading_weights",
                        "runtime_start": "dstack_runtime_starting_runtime",
                        "model_load": "dstack_runtime_loading_model",
                    }
                    raise progress_error(
                        phases.get(report.get("phase"), "dstack_runtime_preparing")
                    )
                _step(
                    "dstack_runtime_report_invalid",
                    validate_report,
                    config,
                    report,
                    manifest,
                )
                gpu = report["gpus"][0]["uuid"]
                _require(GPU_UUID.fullmatch(gpu), "dstack_runtime_physical_gpu_invalid")
                _require(
                    not state.get("physical_gpu_uuid")
                    or state["physical_gpu_uuid"] == gpu,
                    "dstack_runtime_physical_gpu_changed",
                )
                try:
                    host.open_tunnel(config.local_port)
                except BootError as error:
                    raise DstackError(
                        "dstack_runtime_tunnel_port_in_use"
                        if error.args == ("bootstrap_tunnel_port_already_in_use",)
                        else "dstack_runtime_tunnel_failed"
                    ) from None
                except Exception:
                    raise DstackError("dstack_runtime_tunnel_failed") from None
                transport = _step(
                    "dstack_runtime_readiness_unreachable",
                    self.transport_factory,
                    f"http://127.0.0.1:{config.local_port}",
                    private_token_file(directory / "wangp-token"),
                )
                try:
                    native = _step(
                        "dstack_runtime_readiness_unreachable", transport.readiness
                    )
                finally:
                    transport.close()
                _require(
                    isinstance(native, HostReadiness)
                    and native.manifest_digest == config.engine_manifest_digest
                    and native.slot_key == intent_id
                    and type(native.idle) is bool
                    and isinstance(native.incarnation, str)
                    and re.fullmatch(r"[0-9a-f]{32}", native.incarnation),
                    "dstack_runtime_private_endpoint_mismatch",
                )
                for prior in (
                    current.get("runtime_incarnation"),
                    state.get("runtime_incarnation"),
                ):
                    _require(
                        not prior or native.incarnation == prior,
                        "dstack_runtime_incarnation_changed",
                    )
                _step(
                    "dstack_runtime_client_config_changed",
                    _write_immutable,
                    directory / "wangp-manifest.json",
                    manifest,
                )
                _step(
                    "dstack_runtime_client_config_changed",
                    _write_immutable,
                    directory / "wangp-client.json",
                    {
                        "version": 1,
                        "enabled": True,
                        "slot_key": intent_id,
                        "configuration_id": config.configuration_id,
                        "manifest_file": str(directory / "wangp-manifest.json"),
                        "token_file": str(directory / "wangp-token"),
                        "runtime_incarnation": native.incarnation,
                    },
                )
                spec = WorkerSpec(
                    "dstack-" + intent_id.replace("-", ""),
                    config.pool,
                    config.provider,
                    binding["provider_instance_id"],
                    (gpu,),
                    config.recipe_ids,
                    config.model_id,
                    config.configuration_id,
                    "wangp-worker",
                    config.engine_manifest_digest,
                    output_delivery=config.output_delivery,
                    dispatch_backend="hatchet-v1",
                )
                endpoint = f"http://127.0.0.1:{config.local_port}"
                slot = SlotConfig(
                    spec,
                    True,
                    endpoint,
                    (endpoint,),
                    "",
                    native.idle,
                    runtime_config_file=str(directory / "wangp-client.json"),
                )
                # Exercise the original manifest/compiler/factory contract;
                # the parent owns the real WorkerRunner and its transport.
                backend = _step(
                    "dstack_runtime_slot_register_failed",
                    self.backend_factory,
                    slot,
                    directory,
                )
                backend.close()
                state.update(
                    phase="runtime_ready",
                    physical_gpu_uuid=gpu,
                    runtime_incarnation=native.incarnation,
                )
                self._save(receipt, state)
                self._slots[intent_id] = slot
                model = (
                    safe_model_progress(native.preparation.get("model"))
                    if type(native.preparation) is dict
                    else None
                )
                return replace(native, preparation={**preparation, "model": model})
        except DstackError:
            raise
        except OperatorError as error:
            raise DstackError(error.code) from None
        except Exception:
            raise DstackError("dstack_native_runtime_unconfirmed") from None

    def slot(self, intent_id):
        _uuid(intent_id)
        _require(intent_id in self._slots, "dstack_runtime_slot_not_connected")
        return self._slots[intent_id]

    def close(self, intent_id=None):
        """Local sockets only; never stops a leased host or touches its jobs."""
        targets = list(self._hosts) if intent_id is None else [_uuid(intent_id)]
        for identity in targets:
            pair = self._hosts.pop(identity, None)
            self._slots.pop(identity, None)
            if pair is not None:
                pair[2].close()
            if intent_id is not None and self.grant_writer is not None:
                # An ended intent's tunnel user can never reconnect (#859).
                self.grant_writer.revoke(identity)
