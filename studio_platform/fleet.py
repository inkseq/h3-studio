"""CPU-side, explicit multi-slot supervisor. Never provisions a cloud instance.

GPU hosts only run private ComfyUI. Database and object-store credentials stay
on the CPU control host. PIDs are advisory output, never a source for signalling
historical/unrelated processes. The default configuration is disabled.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import asdict, dataclass
import json
import math
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit

from .control import WorkerControl, WorkerSpec, REAL_GPU_BACKENDS, worker_spec_payload
from .repository import Repository, request_hash
from .worker import ComfyBackend, MockBackend, WorkerRunner
from .slot_lock import slot_lock


SAFE_WORKER = re.compile(r"^[A-Za-z0-9_-]{1,80}$")


def _origin(endpoint):
    try:
        parts = urlsplit(endpoint)
        valid = (
            endpoint == endpoint.strip()
            and parts.scheme in ("http", "https")
            and parts.hostname
            and not parts.username
            and not parts.password
            and not parts.query
            and not parts.fragment
            and not parts.path
            and (
                parts.scheme == "https"
                or parts.hostname in ("127.0.0.1", "localhost", "::1")
            )
        )
        parts.port  # Reject invalid/out-of-range ports without echoing the URL.
    except (ValueError, AttributeError):
        valid = False
    if not valid:
        raise ValueError("invalid_fleet_endpoint")
    return endpoint


@dataclass(frozen=True)
class SlotConfig:
    spec: WorkerSpec
    enabled: bool = False
    endpoint: str = ""
    allowed_origins: tuple[str, ...] = ()
    comfy_revision: str = ""
    confirmed_idle: bool = False
    recovery_only: bool = False
    runtime_config_file: str = ""

    def __post_init__(self):
        if (
            not SAFE_WORKER.fullmatch(self.spec.worker_id)
            or type(self.enabled) is not bool
            or type(self.confirmed_idle) is not bool
            or type(self.recovery_only) is not bool
        ):
            raise ValueError("invalid_fleet_slot")
        if self.recovery_only and self.spec.backend not in REAL_GPU_BACKENDS:
            raise ValueError("recovery_requires_real_engine")
        if self.spec.backend == "wangp-worker":
            if (
                not isinstance(self.runtime_config_file, str)
                or not Path(self.runtime_config_file).is_absolute()
            ):
                raise ValueError("absolute_wangp_runtime_config_required")
        elif self.runtime_config_file != "":
            raise ValueError("unexpected_engine_runtime_config")
        if self.spec.backend in {
            "mock",
            "cpu-render",
            "runpod-enhance",
            "everyframe-render",
        }:
            # Serverless enhance reaches RunPod through its own fixed API, not a
            # slot endpoint (#795).
            if self.endpoint or self.allowed_origins or self.comfy_revision:
                raise ValueError("cpu_slot_cannot_have_gpu_endpoint")
        elif self.spec.backend == "wangp-worker":
            if (
                self.comfy_revision
                or not isinstance(self.allowed_origins, tuple)
                or self.allowed_origins != (_origin(self.endpoint),)
            ):
                raise ValueError("explicit_wangp_endpoint_and_manifest_required")
        elif (
            not re.fullmatch(r"[0-9a-f]{40}", self.comfy_revision)
            or not isinstance(self.allowed_origins, tuple)
            or self.allowed_origins != (_origin(self.endpoint),)
        ):
            raise ValueError("explicit_fleet_endpoint_and_revision_required")


@dataclass(frozen=True)
class FleetConfig:
    work_dir: Path
    slots: tuple[SlotConfig, ...] = ()
    enabled: bool = False
    max_children: int = 0
    shutdown_grace_s: float = 210

    def __post_init__(self):
        if not Path(self.work_dir).is_absolute():
            raise ValueError("fleet_work_dir_must_be_absolute")
        object.__setattr__(self, "work_dir", Path(self.work_dir).resolve())
        if (
            type(self.enabled) is not bool
            or not isinstance(self.slots, tuple)
            or len(self.slots) > 32
            or type(self.max_children) is not int
            or not 0 <= self.max_children <= 32
            or not math.isfinite(self.shutdown_grace_s)
            or not 0 < self.shutdown_grace_s <= 3600
        ):
            raise ValueError("invalid_fleet_limits")
        ids, devices, endpoints, backends = set(), set(), set(), set()
        for slot in self.slots:
            if slot.spec.worker_id in ids:
                raise ValueError("duplicate_fleet_worker")
            ids.add(slot.spec.worker_id)
            if not slot.enabled:
                continue
            backends.add(slot.spec.backend)
            if slot.spec.backend in REAL_GPU_BACKENDS:
                if slot.endpoint in endpoints:
                    raise ValueError("duplicate_fleet_endpoint")
                endpoints.add(slot.endpoint)
            for gpu in slot.spec.physical_gpu_ids:
                identity = (slot.spec.provider, slot.spec.instance_id, gpu)
                if identity in devices:
                    raise ValueError("duplicate_fleet_physical_gpu")
                devices.add(identity)
        if "mock" in backends and len(backends) > 1:
            raise ValueError("simulation_and_real_fleets_must_be_separate")
        if self.enabled and sum(s.enabled for s in self.slots) > self.max_children:
            raise ValueError("fleet_child_limit_exceeded")
        if self.enabled and not any(s.enabled for s in self.slots):
            raise ValueError("enabled_fleet_requires_explicit_slot")

    def slot(self, worker_id):
        for slot in self.slots:
            if slot.spec.worker_id == worker_id:
                return slot
        raise ValueError("fleet_slot_not_found")

    def fingerprint(self):
        value = asdict(self)
        value["work_dir"] = str(self.work_dir)
        for item, slot in zip(value["slots"], self.slots):
            item["spec"] = worker_spec_payload(slot.spec)
            if not slot.recovery_only:
                item.pop("recovery_only")
            if not slot.runtime_config_file:
                item.pop("runtime_config_file")
        return request_hash(value)


def read_config(path):
    """Trusted operator file only; no API keys, database URLs or SSH keys belong here."""
    path = Path(path)
    if not path.is_absolute():
        raise ValueError("fleet_config_path_must_be_absolute")
    try:
        with path.open("rb") as source:
            info = os.fstat(source.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or os.name != "nt"
                and info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
            ):
                raise ValueError("fleet_config_must_be_operator_owned")
            raw = source.read(262145)
        if len(raw) > 262144:
            raise ValueError("fleet_config_too_large")
        value = json.loads(raw)
        if (
            not isinstance(value, dict)
            or set(value)
            != {
                "version",
                "work_dir",
                "enabled",
                "max_children",
                "shutdown_grace_s",
                "slots",
            }
            or type(value["version"]) is not int
            or value["version"] not in (1, 2)
            or not isinstance(value["slots"], list)
        ):
            raise ValueError("invalid_fleet_config")
        slots = []
        spec_fields = {
            "worker_id",
            "pool",
            "provider",
            "instance_id",
            "physical_gpu_ids",
            "recipe_ids",
            "model_id",
            "configuration_id",
            "backend",
        }
        for item in value["slots"]:
            required = spec_fields | {
                "enabled",
                "endpoint",
                "allowed_origins",
                "comfy_revision",
                "confirmed_idle",
            }
            optional = {
                "engine_manifest_digest",
                "recovery_only",
                "runtime_config_file",
                "output_delivery",
                "dispatch_backend",
            }
            if (
                not isinstance(item, dict)
                or not required <= set(item)
                or set(item) - required - optional
            ):
                raise ValueError("invalid_fleet_slot_fields")
            if value["version"] == 1 and (
                item["backend"] == "wangp-worker"
                or item.get("engine_manifest_digest", "") != ""
                or item.get("recovery_only", False) is not False
                or item.get("dispatch_backend", "legacy") != "legacy"
                or item.get("runtime_config_file", "") != ""
            ):
                raise ValueError("new_engine_or_recovery_requires_fleet_v2")
            spec = {key: item[key] for key in spec_fields}
            spec["engine_manifest_digest"] = item.get("engine_manifest_digest", "")
            spec["output_delivery"] = item.get("output_delivery", "")
            spec["dispatch_backend"] = item.get("dispatch_backend", "legacy")
            for key in ("physical_gpu_ids", "recipe_ids", "allowed_origins"):
                if not isinstance(item[key], list):
                    raise ValueError("fleet_bindings_must_be_arrays")
            spec["physical_gpu_ids"], spec["recipe_ids"] = (
                tuple(spec["physical_gpu_ids"]),
                tuple(spec["recipe_ids"]),
            )
            slots.append(
                SlotConfig(
                    WorkerSpec(**spec),
                    item["enabled"],
                    item["endpoint"],
                    tuple(item["allowed_origins"]),
                    item["comfy_revision"],
                    item["confirmed_idle"],
                    item.get("recovery_only", False),
                    item.get("runtime_config_file", ""),
                )
            )
        return FleetConfig(
            Path(value["work_dir"]),
            tuple(slots),
            value["enabled"],
            value["max_children"],
            value["shutdown_grace_s"],
        )
    except (
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        KeyError,
        TypeError,
        AttributeError,
    ):
        raise ValueError("fleet_config_unavailable_or_invalid") from None


class FleetSupervisor:
    def __init__(
        self,
        config,
        repository,
        config_path,
        *,
        popen=None,
        clock=time.monotonic,
        sleeper=time.sleep,
    ):
        self.config, self.repo = config, repository
        self.config_path = Path(config_path)
        if not self.config_path.is_absolute():
            raise ValueError("fleet_config_path_must_be_absolute")
        self.popen, self.clock, self.sleeper = popen or subprocess.Popen, clock, sleeper
        self.control = WorkerControl(repository) if repository is not None else None
        self.children = {}
        self._stop = threading.Event()
        self._started = False
        self._finished = False
        self._shutdown_called = False

    def snapshot(self):
        children = []
        for worker_id, proc in self.children.items():
            code = proc.poll()
            children.append(
                {
                    "worker_id": worker_id,
                    "pid": proc.pid,
                    "exit_code": code,
                    "state": "running" if code is None else "exited",
                }
            )
        return {
            "state": "disabled"
            if not self.config.enabled
            else "stopped"
            if self._finished
            else "draining"
            if self._stop.is_set()
            else "running",
            "children": children,
        }

    def _save(self):
        path = self.config.work_dir / "fleet-state.json"
        temporary = path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(self.snapshot(), sort_keys=True), encoding="utf-8"
        )
        temporary.replace(path)

    def start(self):
        if not self.config.enabled:
            return self.snapshot()
        if self._started:
            raise ValueError("fleet_already_started")
        if self._stop.is_set() or self.repo is None:
            raise ValueError("fleet_not_admitting")
        if (self.config.work_dir / "supervisor-drain.flag").exists():
            raise ValueError("fleet_drain_marker_requires_explicit_reset")
        self.repo.create_schema()
        active = [slot for slot in self.config.slots if slot.enabled]
        if all(s.spec.backend == "mock" for s in active):
            from .repository import capacity_gate

            with self.repo.engine.connect() as conn:
                has_gate = conn.execute(capacity_gate.select()).first() is not None
            if not has_gate:
                self.repo.configure_capacity()  # Zero real capacity, never an approval.
        # No process starts until all configured physical identities pass the
        # existing global ledger gate. A partial registration holds ownership;
        # it neither creates cloud resources nor guesses that an instance is idle.
        for slot in active:
            if slot.recovery_only:
                self.control.require_recovery_binding(slot.spec)
            else:
                self.control.register(slot.spec)
        self.config.work_dir.mkdir(parents=True, exist_ok=True)
        self._started = True
        try:
            for slot in active:
                directory = self.config.work_dir / slot.spec.worker_id
                directory.mkdir(parents=True, exist_ok=True)
                flag = directory / "drain.flag"
                if flag.exists() and not slot.recovery_only:
                    flag.unlink()  # Only our explicit lifecycle marker, never an asset.
                kwargs = dict(stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                if os.name == "nt":
                    kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
                else:
                    kwargs["start_new_session"] = True
                proc = self.popen(
                    [
                        sys.executable,
                        "-m",
                        "studio_platform.fleet",
                        "--config",
                        str(self.config_path),
                        "--slot",
                        slot.spec.worker_id,
                        "--config-hash",
                        self.config.fingerprint(),
                    ],
                    **kwargs,
                )
                self.children[slot.spec.worker_id] = proc
            self._save()
            return self.snapshot()
        except Exception:
            self.drain()
            raise RuntimeError("fleet_process_start_failed") from None

    def tick(self):
        if not self.config.enabled:
            return self.snapshot()
        if not self._started:
            raise ValueError("fleet_not_started")
        if (self.config.work_dir / "supervisor-drain.flag").exists():
            self._stop.set()
        self.control.recover_expired()
        # No automatic restart. An exited process is not proof of upstream idle.
        for worker_id, proc in self.children.items():
            if proc.poll() is not None:
                self.control.drain(worker_id)
        self._save()
        return self.snapshot()

    def drain(self):
        self._stop.set()
        if not self.config.enabled or not self._started:
            return self.snapshot()
        for worker_id, proc in self.children.items():
            (self.config.work_dir / worker_id / "drain.flag").touch()
            try:
                self.control.drain(worker_id)
            except Exception:
                pass  # Lease still expires conservatively, never to idle.
            if os.name != "nt" and proc.poll() is None:
                try:
                    proc.send_signal(signal.SIGTERM)
                except ProcessLookupError:
                    pass
        self._save()
        return self.snapshot()

    def shutdown(self):
        self._shutdown_called = True
        self.drain()
        deadline = self.clock() + self.config.shutdown_grace_s
        while (
            any(proc.poll() is None for proc in self.children.values())
            and self.clock() < deadline
        ):
            self.sleeper(min(0.1, max(0, deadline - self.clock())))
        self._finished = all(proc.poll() is not None for proc in self.children.values())
        self._save() if self._started else None
        result = self.snapshot()
        if any(proc.poll() is None for proc in self.children.values()):
            result["state"] = "drain_pending"
        return result  # No blind PID kill or cloud destruction at a timeout.

    def run_forever(self, *, poll_interval_s=1):
        if not self.config.enabled:
            return self.snapshot()
        if not math.isfinite(poll_interval_s) or not 0.01 <= poll_interval_s <= 60:
            raise ValueError("invalid_fleet_poll_interval")
        self.config.work_dir.mkdir(parents=True, exist_ok=True)
        with slot_lock(self.config.work_dir, "fleet-supervisor") as acquired:
            if not acquired:
                return {"state": "fleet_busy", "children": []}
            prior = {}
            if threading.current_thread() is threading.main_thread():
                for signum in (signal.SIGTERM, signal.SIGINT):
                    prior[signum] = signal.getsignal(signum)
                    signal.signal(signum, lambda *_: self._stop.set())
            try:
                self.start()
                while not self._stop.is_set():
                    self.tick()
                    if self.children and all(
                        proc.poll() is not None for proc in self.children.values()
                    ):
                        self._stop.set()
                        break
                    self._stop.wait(poll_interval_s)
                return self.shutdown()
            finally:
                try:
                    if self._started and not self._shutdown_called:
                        self.shutdown()
                finally:
                    for signum, handler in prior.items():
                        signal.signal(signum, handler)


def create_store(settings):
    """Same explicit CPU-side storage selection as the business API."""
    from .storage import LocalObjectStore, S3ObjectStore

    if settings.storage_provider == "local":
        return LocalObjectStore(settings.data_dir / "objects")
    if settings.storage_provider != "r2":
        raise ValueError("fleet_storage_requires_reviewed_runtime_adapter")
    from .storage_config import S3StorageConfig, load_configured_storage_credentials

    config = S3StorageConfig(
        "r2",
        settings.storage_endpoint,
        settings.storage_region,
        settings.storage_bucket,
        "cloudflare-r2",
        settings.storage_profile,
        enabled=True,
    )
    credentials = load_configured_storage_credentials(
        config,
        credentials_file=settings.storage_credential_file,
        registry_root=os.environ.get("AI_REGISTRY_ROOT"),
    )
    return S3ObjectStore(config, credentials)


def request_drain(config):
    """Portable operator-only stop request. Never reads/signals a saved PID."""
    if not config.enabled:
        return {"state": "disabled", "children": []}
    if not config.work_dir.is_dir():
        raise ValueError("fleet_runtime_directory_not_found")
    (config.work_dir / "supervisor-drain.flag").touch()
    return {"state": "drain_requested"}


def _require_service_backend(slot, settings):
    """The slot's backend must be the one this service explicitly enabled."""
    backend = slot.spec.backend
    if slot.recovery_only:
        if backend not in settings.recovery_backends:
            raise ValueError("fleet_recovery_backend_not_explicitly_allowed")
        return
    switches = {
        "cpu-render": "render_enabled",
        "runpod-enhance": "enhance_enabled",
        "everyframe-render": "everyframe_enabled",
    }
    if backend in switches:
        allowed = getattr(settings, switches[backend])
    else:
        allowed = settings.execution_backend == backend and settings.generation_enabled
    if not allowed:
        raise ValueError("fleet_backend_does_not_match_explicit_service_settings")


@contextmanager
def slot_runner(
    config,
    worker_id,
    settings,
    *,
    repository=None,
    store_factory=create_store,
    backend_factory=None,
    runner_factory=WorkerRunner,
    selected_job_id=None,
    capacity_usage=None,
):
    """Explicit CPU worker child; injectable factories permit fully offline tests."""
    slot = config.slot(worker_id)
    if not config.enabled or not slot.enabled:
        yield None
        return
    _require_service_backend(slot, settings)
    own_repo = repository is None
    repo = repository or Repository(settings.database_url)
    backend = None
    try:
        repo.create_schema()
        control = WorkerControl(repo, capacity_usage=capacity_usage)
        if slot.spec.backend == "mock":
            from .repository import capacity_gate

            with repo.engine.connect() as conn:
                has_gate = conn.execute(capacity_gate.select()).first() is not None
            if not has_gate:
                repo.configure_capacity()
        worker = (
            control.require_recovery_binding(slot.spec)
            if slot.recovery_only
            else control.register(slot.spec)
        )
        directory = config.work_dir / worker_id
        if backend_factory:
            backend = backend_factory(slot, directory)
        elif slot.spec.backend == "mock":
            backend = MockBackend(directory / "simulation", enabled=True)
        elif slot.spec.backend == "cpu-render":
            from .render_backend import CPURenderBackend

            backend = CPURenderBackend(directory / "render", enabled=True)
        elif slot.spec.backend == "runpod-enhance":
            from . import enhance_backend

            backend = enhance_backend.create(store_factory(settings), settings)
        elif slot.spec.backend == "everyframe-render":
            from .providers import everyframe

            backend = everyframe.create(
                store_factory(settings), settings, slot_key=slot.spec.worker_id
            )
        elif slot.spec.backend == "comfy-worker":
            backend = ComfyBackend(
                endpoint=slot.endpoint,
                enabled=True,
                allowed_origins=slot.allowed_origins,
                comfy_revision=slot.comfy_revision,
            )
        else:
            # Fixed, protected configuration loader; never import a user-supplied
            # factory or route an unconfigured new engine through Comfy.
            from .inference.wangp_factory import create_backend

            backend = create_backend(slot, directory)
        if (
            getattr(backend, "kind", None) != slot.spec.backend
            or getattr(backend, "enabled", None) is not True
        ):
            raise ValueError("fleet_adapter_identity_mismatch")
        if (
            slot.spec.backend == "wangp-worker"
            and getattr(getattr(backend, "manifest", None), "digest", None)
            != slot.spec.engine_manifest_digest
        ):
            raise ValueError("fleet_adapter_manifest_mismatch")
        if (
            not slot.recovery_only
            and worker["current_job_id"] is None
            and worker["state"] != "retired"
            and not (
                slot.spec.dispatch_backend == "hatchet-v1" and worker["drain_requested"]
            )
        ):
            if slot.spec.backend in {
                "mock",
                "cpu-render",
                "runpod-enhance",
                "everyframe-render",
            }:
                control.mark_ready(worker_id, upstream_idle_confirmed=True)
            elif slot.confirmed_idle:
                # A startup declaration alone is insufficient: the dedicated
                # engine must currently confirm idle through its adapter.
                if backend.is_idle() is not True:
                    raise ValueError("fleet_upstream_idle_not_confirmed")
                control.mark_ready(worker_id, upstream_idle_confirmed=True)
            elif worker["state"] != "ready" or worker["expires_at"] <= repo.clock():
                raise ValueError("fleet_readiness_requires_explicit_confirmation")
        store = store_factory(settings)
        from .execution_policy import ExecutionPolicies

        recovery_kwargs = {}
        if slot.recovery_only:
            from .drain_safe_runner import DrainSafeRunner

            if runner_factory is WorkerRunner:
                runner_factory = DrainSafeRunner
            elif not isinstance(runner_factory, type) or not issubclass(
                runner_factory, DrainSafeRunner
            ):
                raise ValueError("recovery_requires_drain_safe_runner")
            recovery_kwargs = {
                "stop_new": lambda: True,
                "job_allowed": lambda job: False,
                "collection_lock_dir": config.work_dir / "recovery-collection",
            }
        telemetry = None
        if (
            not slot.recovery_only
            and slot.spec.dispatch_backend == "hatchet-v1"
            and runner_factory is WorkerRunner
        ):
            from .postgres_dispatch import PostgresSlotRunner
            from .telemetry import configured_telemetry

            runner_factory = PostgresSlotRunner
            telemetry = configured_telemetry()
            recovery_kwargs.update(
                collection_lock_dir=config.work_dir / "collection-lock",
                telemetry=telemetry,
            )
        try:
            runner = runner_factory(
                repo,
                store,
                directory,
                backend=backend,
                control=control,
                submission_guard=ExecutionPolicies(settings, repo).submission_allowed,
                stop_requested=lambda: (directory / "drain.flag").exists(),
                selected_job_id=selected_job_id,
                **recovery_kwargs,
            )
            yield runner
        finally:
            if telemetry is not None:
                telemetry.close()
    finally:
        if backend is not None and hasattr(backend, "close"):
            backend.close()
        if own_repo:
            repo.close()


def run_slot(config, worker_id, settings, *, once=False, **kwargs):
    """CLI compatibility; the DBOS executor uses the same bounded domain runner."""
    with slot_runner(config, worker_id, settings, **kwargs) as runner:
        if runner is None:
            return {"state": "disabled", "simulation": False}
        pool = config.slot(worker_id).spec.pool
        return (
            runner.run_once(worker_id, pool)
            if once
            else runner.run_forever(worker_id, pool)
        )


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Explicit CPU fleet; cloud creation remains disabled"
    )
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--slot")
    parser.add_argument("--config-hash")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--request-drain", action="store_true")
    args = parser.parse_args(argv)
    repo = None
    try:
        config = read_config(args.config)
        if args.config_hash is not None and args.config_hash != config.fingerprint():
            raise ValueError("fleet_config_changed_before_child_start")
        if not config.enabled:
            print(json.dumps({"state": "disabled", "children": []}))
            return 0
        if args.request_drain:
            if args.slot or args.once:
                raise ValueError("drain_request_cannot_start_workers")
            print(json.dumps(request_drain(config)))
            return 0
        from .settings import Settings

        settings = Settings.from_environment()
        if args.slot:
            result = run_slot(config, args.slot, settings, once=args.once)
        else:
            if args.once:
                raise ValueError("supervisor_once_would_leave_unmanaged_children")
            repo = Repository(settings.database_url)
            result = FleetSupervisor(config, repo, args.config).run_forever()
        print(json.dumps(result))
        return 1 if result and result.get("state") == "drain_pending" else 0
    except Exception:
        print(json.dumps({"state": "fleet_configuration_or_runtime_error"}))
        return 1
    finally:
        if repo:
            repo.close()


if __name__ == "__main__":
    raise SystemExit(main())
