"""Finite production acceptance on one explicitly handed-off existing GPU.

No Lium credential, provider API, rental, model installation or cloud deletion.
The original controller remains the sole TTL/destruction owner. Default CLI
validates a protected operator config without opening DB, SSH or backend.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys
import time

from sqlalchemy import and_, select

from .control import WorkerControl, WorkerSpec
from .drain_safe_runner import DrainSafeRunner
from .fleet import (
    FleetConfig,
    FleetSupervisor,
    SlotConfig,
    read_config as read_fleet,
    run_slot,
)
from .lium_bootstrap import SSHHost, COMFY_REVISION, MODEL_REVISION
from .repository import Repository, attempts, jobs, registered_workers
from .settings import Settings
from .worker import ComfyBackend, _slot_lock


MODEL = "MiniMax-H3-Base-BF16"
RECIPE = "h3-base-fl2va-v1"
SAFE_ID = re.compile(r"[A-Za-z0-9_-]{1,80}")
UUID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")


class AcceptanceError(ValueError):
    pass


@dataclass(frozen=True)
class AcceptanceConfig:
    version: int
    enabled: bool
    handoff_id: str
    work_dir: Path
    ssh_key_file: Path
    known_hosts_file: Path
    host: str
    ssh_port: int
    local_port: int
    boot_identity: dict
    gpu_uuid: str
    worker_id: str
    pool: str
    hard_deadline: float
    drain_margin_s: int
    qualification_evidence_id: str
    owner: str = "superdan"
    tenant: str = "sixnine"
    collection_margin_s: int = 120

    @property
    def trust_first_host_key(self):
        return False

    @property
    def stop_claiming_at(self):
        return self.hard_deadline - self.drain_margin_s

    @property
    def configuration_id(self):
        return self.boot_identity["configuration_id"]

    def __post_init__(self):
        if (
            type(self.version) is not int
            or self.version != 1
            or type(self.enabled) is not bool
            or self.owner != "superdan"
            or self.tenant != "sixnine"
        ):
            raise AcceptanceError("acceptance_identity_invalid")
        for value in (
            self.handoff_id,
            self.worker_id,
            self.pool,
            self.qualification_evidence_id,
        ):
            if not isinstance(value, str) or not SAFE_ID.fullmatch(value):
                raise AcceptanceError("acceptance_identifier_invalid")
        for field in ("work_dir", "ssh_key_file", "known_hosts_file"):
            path = Path(getattr(self, field))
            if not path.is_absolute():
                raise AcceptanceError("acceptance_paths_must_be_absolute")
            object.__setattr__(self, field, path)
        try:
            host = ipaddress.ip_address(self.host)
            if not host.is_global or str(host) != self.host:
                raise ValueError
        except (ValueError, TypeError):
            raise AcceptanceError("acceptance_public_ssh_address_required") from None
        if (
            type(self.ssh_port) is not int
            or not 1 <= self.ssh_port <= 65535
            or type(self.local_port) is not int
            or not 1024 <= self.local_port <= 65535
            or type(self.hard_deadline) not in (int, float)
            or not math.isfinite(self.hard_deadline)
            or type(self.drain_margin_s) is not int
            or not 120 <= self.drain_margin_s <= 3600
            or type(self.collection_margin_s) is not int
            or not 30 <= self.collection_margin_s <= 900
            or not isinstance(self.gpu_uuid, str)
            or not re.fullmatch(r"GPU-[A-Za-z0-9-]{8,100}", self.gpu_uuid)
        ):
            raise AcceptanceError("acceptance_limits_invalid")
        identity = self.boot_identity
        if (
            not isinstance(identity, dict)
            or set(identity)
            != {"intent_id", "instance_id", "configuration_id", "sources"}
            or any(
                not isinstance(identity[x], str) or not UUID.fullmatch(identity[x])
                for x in ("intent_id", "instance_id")
            )
            or not isinstance(identity["configuration_id"], str)
            or not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", identity["configuration_id"])
            or not isinstance(identity["sources"], dict)
            or set(identity["sources"]) != {"bootstrap_cloud.py", "model_manifest.json"}
            or any(
                not isinstance(v, str) or not re.fullmatch(r"[0-9a-f]{64}", v)
                for v in identity["sources"].values()
            )
        ):
            raise AcceptanceError("acceptance_original_boot_identity_required")

    def identity(self):
        return {
            "handoff_id": self.handoff_id,
            "worker_id": self.worker_id,
            "pool": self.pool,
            "boot_identity": self.boot_identity,
            "gpu_uuid": self.gpu_uuid,
            "hard_deadline": self.hard_deadline,
            "drain_margin_s": self.drain_margin_s,
            "collection_margin_s": self.collection_margin_s,
            "local_port": self.local_port,
            "qualification_evidence_id": self.qualification_evidence_id,
            "tenant": self.tenant,
            "owner": self.owner,
        }


def read_config(path):
    path = Path(path)
    if not path.is_absolute():
        raise AcceptanceError("acceptance_config_path_must_be_absolute")
    try:
        with path.open("rb") as source:
            info = os.fstat(source.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or os.name != "nt"
                and info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
            ):
                raise AcceptanceError("acceptance_config_must_be_protected")
            raw = source.read(32769)
        if len(raw) > 32768:
            raise AcceptanceError("acceptance_config_too_large")
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError
        return AcceptanceConfig(**value)
    except AcceptanceError:
        raise
    except (OSError, ValueError, TypeError):
        raise AcceptanceError("acceptance_config_unavailable_or_invalid") from None


def save(path, value):
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as out:
        json.dump(value, out, sort_keys=True)
        out.flush()
        os.fsync(out.fileno())
    temporary.replace(path)


def validate_settings(config, settings):
    if (
        settings.tenant_id != config.tenant
        or settings.auth_mode != "password"
        or settings.execution_backend != "comfy-worker"
        or not settings.generation_enabled
        or settings.storage_provider != "local"
        or settings.public_origin
        not in {"https://inkseq.com", "https://www.sixnine.art"}
        or not settings.database_url.startswith("postgresql+psycopg:")
        or settings.execution_policy_file is None
    ):
        raise AcceptanceError("acceptance_requires_exact_production_settings")
    from .execution_policy import read_policy

    policy = read_policy(settings.execution_policy_file)
    if (
        not policy
        or policy["pool"] != config.pool
        or policy["configuration_id"] != config.configuration_id
        or policy["model_id"] != MODEL
        or policy["recipe_ids"] != [RECIPE]
        or policy["qualification"]["evidence_id"] != config.qualification_evidence_id
        or policy["qualification"]["expires_at"] > config.hard_deadline
        or policy["reservation"]["expires_at"] > config.hard_deadline
    ):
        raise AcceptanceError("acceptance_policy_identity_or_deadline_mismatch")


def verify_report(config, report):
    if (
        report.get("identity") != config.boot_identity
        or report.get("state") != "ready"
        or report.get("model_revision") != MODEL_REVISION
        or report.get("comfyui_revision") != COMFY_REVISION
        or report.get("actual_comfy_revision") != COMFY_REVISION
    ):
        raise AcceptanceError("acceptance_remote_boot_identity_mismatch")
    gpus, files = report.get("gpus", []), report.get("files", {})
    if (
        not isinstance(gpus, list)
        or len(gpus) != 1
        or gpus[0].get("uuid") != config.gpu_uuid
        or type(report.get("runtime", {}).get("gpu_total_bytes")) is not int
        or report["runtime"]["gpu_total_bytes"] < 90 * 1024**3
        or not isinstance(files, dict)
        or len(files) != 5
        or any(
            not isinstance(v, dict)
            or v.get("state") != "verified_size"
            or v.get("revision") != MODEL_REVISION
            or type(v.get("size_bytes")) is not int
            or v["size_bytes"] <= 0
            for v in files.values()
        )
    ):
        raise AcceptanceError("acceptance_remote_gpu_or_weights_mismatch")


def reserve_remote(host, config):
    # Created before Fleet starts. Any existing marker, even our own, requires
    # explicit recovery; a lost response cannot cause a second CPU controller.
    script = '''import json,os
from pathlib import Path
p=Path('/workspace/h3-studio/sixnine-production-worker.json')
with p.open('x') as out:
 json.dump(IDENTITY,out);out.flush();os.fsync(out.fileno())
print(json.dumps({'reserved':True}))
'''.replace("IDENTITY", repr(config.identity()))
    if host.run(script) != {"reserved": True}:
        raise AcceptanceError("acceptance_remote_reservation_unconfirmed")


def ledger_status(repo, config):
    now = repo.clock()
    with repo.engine.connect() as conn:
        worker = (
            conn.execute(
                select(registered_workers).where(
                    registered_workers.c.id == config.worker_id
                )
            )
            .mappings()
            .first()
        )
        active = sorted(
            set(
                conn.execute(
                    select(jobs.c.id)
                    .join(attempts, attempts.c.job_id == jobs.c.id)
                    .where(
                        attempts.c.worker_id == config.worker_id,
                        jobs.c.status.not_in(("succeeded", "failed", "cancelled")),
                    )
                ).scalars()
            )
        )
    current = worker["current_job_id"] if worker else None
    if current and current not in active:
        active.append(current)
    matches = bool(
        worker
        and worker["pool"] == config.pool
        and worker["provider"] == "lium"
        and worker["instance_id"] == config.boot_identity["instance_id"]
        and worker["spec"]["configuration_id"] == config.configuration_id
        and worker["spec"]["physical_gpu_ids"] == [config.gpu_uuid]
    )
    safe = bool(
        matches
        and worker["state"] == "draining"
        and worker["drain_requested"]
        and worker["expires_at"] > now
        and current is None
        and not active
    )
    return {
        "ledger_safe": safe,
        "active_job_ids": active,
        "worker_state": worker["state"] if worker else "missing",
        "worker_drain_requested": bool(matches and worker["drain_requested"]),
    }


def request_drain(repo, config):
    if not config.work_dir.is_dir():
        raise AcceptanceError("acceptance_runtime_missing")
    (config.work_dir / "drain.flag").touch()
    control = WorkerControl(repo)
    worker = control.get(config.worker_id)
    if (
        worker["pool"] != config.pool
        or worker["instance_id"] != config.boot_identity["instance_id"]
        or worker["spec"]["configuration_id"] != config.configuration_id
        or worker["state"] in ("unknown", "retired")
        or worker["expires_at"] <= repo.clock()
    ):
        raise AcceptanceError("acceptance_drain_requires_current_exact_worker")
    # Uses the same durable worker-row lock as claim; blocks new generation
    # before a restore process can rely on a subsequently observed idle result.
    control.drain(config.worker_id)
    return {
        "phase": "drain_requested",
        "handoff_id": config.handoff_id,
        "worker_ids": [config.worker_id],
        "drained": False,
    }


class AcceptanceRunner(DrainSafeRunner):
    """Stop NEW work at the deadline; keep collecting/reconciling old attempts."""

    def __init__(self, *args, acceptance, **kwargs):
        self.acceptance = acceptance
        super().__init__(
            *args,
            **kwargs,
            stop_new=lambda: (
                self.repo.clock() >= acceptance.stop_claiming_at
                or (acceptance.work_dir / "drain.flag").exists()
            ),
            job_allowed=self._acceptance_job_allowed,
            job_filter=and_(
                jobs.c.tenant_id == acceptance.tenant,
                jobs.c.owner_id == acceptance.owner,
                jobs.c.pool == acceptance.pool,
                jobs.c.execution_plan["configuration_id"].as_string()
                == acceptance.configuration_id,
            ),
            collection_lock_dir=acceptance.work_dir / "collection-lock",
        )

    def _acceptance_job_allowed(self, job):
        duration = job.get("expected_runtime_s")
        return bool(
            type(duration) in (int, float)
            and math.isfinite(duration)
            and duration > 0
            and self.repo.clock() + duration + self.acceptance.collection_margin_s
            < self.acceptance.hard_deadline
            and job["tenant_id"] == self.acceptance.tenant
            and job["owner_id"] == self.acceptance.owner
        )


class ProductionController:
    def __init__(
        self,
        repo,
        config,
        config_path,
        *,
        host_factory=SSHHost,
        backend_factory=ComfyBackend,
        fleet_factory=FleetSupervisor,
    ):
        self.repo, self.config, self.path = repo, config, Path(config_path)
        self.host_factory, self.backend_factory, self.fleet_factory = (
            host_factory,
            backend_factory,
            fleet_factory,
        )
        self.host = self.backend = self.fleet = None

    def _popen(self, argv, **kwargs):
        # Fleet owns process handles/drain, but the child uses the finite-deadline
        # runner above instead of exiting before unresolved collection is done.
        expected = [
            sys.executable,
            "-m",
            "studio_platform.fleet",
            "--config",
            str(self.config.work_dir / "fleet.json"),
            "--slot",
            self.config.worker_id,
            "--config-hash",
            self.fleet.config.fingerprint(),
        ]
        if argv != expected:
            raise AcceptanceError("acceptance_unexpected_child_command")
        return subprocess.Popen(
            [
                sys.executable,
                "-m",
                "studio_platform.production_worker",
                "--config",
                str(self.path),
                "--enabled",
                "--slot",
                "--config-hash",
                self.fleet.config.fingerprint(),
            ],
            **kwargs,
        )

    def start(self):
        config = self.config
        if (
            not config.enabled
            or self.repo.clock() >= config.stop_claiming_at
            or (config.work_dir / "drain.flag").exists()
        ):
            raise AcceptanceError("acceptance_not_admitting")
        config.work_dir.mkdir(parents=True, exist_ok=True)
        receipt = config.work_dir / "acceptance-state.json"
        if receipt.exists():
            raise AcceptanceError("acceptance_recovery_required")
        # Host-key trust cannot be added during acceptance.
        if not config.known_hosts_file.is_file() or not config.ssh_key_file.is_file():
            raise AcceptanceError("acceptance_ssh_identity_missing")
        if os.name != "nt" and config.ssh_key_file.stat().st_mode & (
            stat.S_IRWXG | stat.S_IRWXO
        ):
            raise AcceptanceError("acceptance_ssh_private_key_permissions_too_broad")
        self.host = self.host_factory(
            config, {"host": config.host, "port": config.ssh_port}
        )
        verify_report(config, self.host.report())
        self.host.open_tunnel(config.local_port)
        endpoint = f"http://127.0.0.1:{config.local_port}"
        self.backend = self.backend_factory(
            endpoint=endpoint,
            enabled=True,
            allowed_origins=(endpoint,),
            comfy_revision=COMFY_REVISION,
        )
        if not self.upstream_idle():
            raise AcceptanceError("acceptance_upstream_not_idle")
        save(receipt, {"identity": config.identity(), "phase": "remote_reserving"})
        reserve_remote(self.host, config)
        spec = WorkerSpec(
            config.worker_id,
            config.pool,
            "lium",
            config.boot_identity["instance_id"],
            (config.gpu_uuid,),
            (RECIPE,),
            MODEL,
            config.configuration_id,
        )
        slot = SlotConfig(spec, True, endpoint, (endpoint,), COMFY_REVISION, True)
        fleet = FleetConfig(config.work_dir / "fleet", (slot,), True, 1)
        value = {
            "version": 1,
            "work_dir": str(fleet.work_dir),
            "enabled": True,
            "max_children": 1,
            "shutdown_grace_s": fleet.shutdown_grace_s,
            "slots": [
                {
                    **spec.__dict__,
                    "enabled": True,
                    "endpoint": endpoint,
                    "allowed_origins": [endpoint],
                    "comfy_revision": COMFY_REVISION,
                    "confirmed_idle": True,
                }
            ],
        }
        save(config.work_dir / "fleet.json", value)
        self.fleet = self.fleet_factory(
            fleet, self.repo, config.work_dir / "fleet.json", popen=self._popen
        )
        save(receipt, {"identity": config.identity(), "phase": "fleet_starting"})
        self.fleet.start()
        save(receipt, {"identity": config.identity(), "phase": "fleet_started"})

    def upstream_idle(self):
        value = self.backend._json("GET", "/queue")
        return value.get("queue_running") == [] and value.get("queue_pending") == []

    def tick(self):
        config = self.config
        if self.repo.clock() >= config.stop_claiming_at:
            (config.work_dir / "drain.flag").touch()
        draining = (config.work_dir / "drain.flag").exists()
        if draining:
            self.fleet.drain()
        fleet = self.fleet.tick()
        state = ledger_status(self.repo, config)
        idle = False
        try:
            idle = self.upstream_idle()
        except Exception:
            pass
        drained = draining and state["ledger_safe"] and idle
        result = {
            "version": 1,
            "handoff_id": config.handoff_id,
            "hard_deadline": config.hard_deadline,
            "worker_ids": [config.worker_id],
            "observed_at": self.repo.clock(),
            "drain_requested": draining,
            "upstream_idle_confirmed": idle,
            "drained": bool(drained),
            **state,
            "phase": "drained" if drained else "draining" if draining else "running",
            "children": [
                {"worker_id": x["worker_id"], "state": x["state"]}
                for x in fleet["children"]
            ],
        }
        if any(x["state"] != "running" for x in result["children"]) and not drained:
            result["phase"] = "worker_attention_required"
        save(config.work_dir / "worker-status.json", result)
        return result

    def close(self):
        # No kill, no cloud operation. Normal loop only closes after proven drain.
        if self.backend:
            self.backend.close()
        if self.host:
            self.host.close()


def status(repo, config, *, host_factory=SSHHost):
    result = {
        "handoff_id": config.handoff_id,
        "worker_ids": [config.worker_id],
        "hard_deadline": config.hard_deadline,
        "drained": False,
        "phase": "unknown",
        "observed_at": repo.clock(),
    }
    try:
        cached = json.loads((config.work_dir / "worker-status.json").read_text())
        receipt = json.loads((config.work_dir / "acceptance-state.json").read_text())
        if (
            receipt["identity"] != config.identity()
            or cached["handoff_id"] != config.handoff_id
            or cached["worker_ids"] != [config.worker_id]
            or cached["hard_deadline"] != config.hard_deadline
        ):
            return result
        ledger = ledger_status(repo, config)
        result = {
            **cached,
            **ledger,
            "drained": False,
            "upstream_idle_confirmed": False,
            "observed_at": repo.clock(),
            "drain_requested": (config.work_dir / "drain.flag").exists()
            and ledger["worker_drain_requested"],
        }
        if not ledger["ledger_safe"] or not (config.work_dir / "drain.flag").exists():
            return result
        # This read-only path also works from a one-off CPU container after the
        # original worker exited. Never opens a tunnel, registers or submits.
        host = host_factory(config, {"host": config.host, "port": config.ssh_port})
        try:
            observed = host.run('''import json,urllib.request
from pathlib import Path
identity=json.loads(Path('/workspace/h3-studio/sixnine-production-worker.json').read_text())
q=json.load(urllib.request.urlopen('http://127.0.0.1:8188/queue',timeout=5))
print(json.dumps({'identity':identity,'idle':q.get('queue_running')==[] and q.get('queue_pending')==[]}))
''')
        finally:
            host.close()
        result["upstream_idle_confirmed"] = observed == {
            "identity": config.identity(),
            "idle": True,
        }
        # Re-read the ledger after SSH observation, not just its earlier state.
        result.update(ledger_status(repo, config))
        result["drain_requested"] = (
            config.work_dir / "drain.flag"
        ).exists() and result["worker_drain_requested"]
        result["drained"] = result["ledger_safe"] and result["upstream_idle_confirmed"]
        result["observed_at"] = repo.clock()
    except Exception:
        pass
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--enabled", action="store_true")
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--request-drain", action="store_true")
    actions.add_argument("--status", action="store_true")
    actions.add_argument("--slot", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--config-hash", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    repo = controller = None
    prior = {}
    try:
        config = read_config(args.config)
        if (
            not args.status
            and not args.request_drain
            and (not args.enabled or not config.enabled)
        ):
            print(
                json.dumps(
                    {
                        "phase": "disabled",
                        "config_valid": True,
                        "cloud_creation_enabled": False,
                    }
                )
            )
            return 0
        settings = Settings.from_environment()
        validate_settings(config, settings)
        repo = Repository(settings.database_url)
        if args.request_drain:
            print(json.dumps(request_drain(repo, config)))
            return 0
        if args.status:
            print(json.dumps(status(repo, config)))
            return 0
        if args.slot:
            fleet = read_fleet(config.work_dir / "fleet.json")
            receipt = json.loads(
                (config.work_dir / "acceptance-state.json").read_text()
            )
            if (
                not args.config_hash
                or fleet.fingerprint() != args.config_hash
                or receipt["identity"] != config.identity()
                or receipt["phase"] not in ("fleet_starting", "fleet_started")
            ):
                raise AcceptanceError("acceptance_child_config_mismatch")
            run_slot(
                fleet,
                config.worker_id,
                settings,
                repository=repo,
                runner_factory=lambda *a, **kw: AcceptanceRunner(
                    *a, acceptance=config, **kw
                ),
            )
            return 0
        config.work_dir.mkdir(parents=True, exist_ok=True)
        with _slot_lock(config.work_dir, "production-acceptance") as acquired:
            if not acquired:
                raise AcceptanceError("acceptance_already_running")
            controller = ProductionController(repo, config, args.config)
            controller.start()
            for signum in (signal.SIGTERM, signal.SIGINT):
                prior[signum] = signal.getsignal(signum)
                signal.signal(
                    signum, lambda *_: (config.work_dir / "drain.flag").touch()
                )
            while True:
                try:
                    value = controller.tick()
                except Exception:
                    # Preserve the CPU control process/tunnel for an unresolved
                    # upstream task. Do not turn an observation error into kill.
                    save(
                        config.work_dir / "worker-status.json",
                        {
                            "version": 1,
                            "handoff_id": config.handoff_id,
                            "worker_ids": [config.worker_id],
                            "hard_deadline": config.hard_deadline,
                            "observed_at": repo.clock(),
                            "phase": "observation_unknown",
                            "drained": False,
                        },
                    )
                    time.sleep(2)
                    continue
                if value["drained"]:
                    return 0
                time.sleep(2)
    except Exception as error:
        print(
            json.dumps(
                {
                    "phase": "acceptance_error_or_recovery_required",
                    "drained": False,
                    "cloud_creation_enabled": False,
                    "error_code": str(error)
                    if isinstance(error, AcceptanceError)
                    else "acceptance_runtime_unconfirmed",
                }
            )
        )
        return 1
    finally:
        for signum, handler in prior.items():
            signal.signal(signum, handler)
        if controller:
            controller.close()
        if repo:
            repo.close()


if __name__ == "__main__":
    raise SystemExit(main())
