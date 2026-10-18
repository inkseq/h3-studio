"""Explicit operator-authorized reuse of one idle, pinned existing GPU runtime.

This module cannot rent, start remote setup, mutate its historical identity,
cancel tasks, fetch synthetic outputs, or submit inference. A root-owned bridge
names every known legacy synthetic task; each is read-only reconciled before a
fresh queued-task receipt can be written. The original marker remains intact.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
from os import fstat
from pathlib import Path
import re
import stat

from .lium_bootstrap import BootError, COMFY_REVISION, MODEL_REVISION
from .qualification_profiles import QUEUED_TASK_PROFILE
from .repository import request_hash

MODEL = "MiniMax-H3-Base-BF16"
HASH = re.compile(r"[0-9a-f]{64}")
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
FIELDS = {
    "version",
    "profile",
    "old_identity",
    "new_identity",
    "model_id",
    "model_revision",
    "comfyui_revision",
    "physical_gpu_uuid",
    "prior_tasks",
    "issued_at",
    "expires_at",
    "operator_config_hash",
    "ledger_handoff_sha256",
}


def _read(path, *, operator=False):
    path = Path(path)
    if not path.is_absolute() or path.is_symlink():
        raise BootError("runtime_adoption_file_untrusted")
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        with os.fdopen(os.open(path, flags), "rb") as handle:
            meta = fstat(handle.fileno())
            if (
                not stat.S_ISREG(meta.st_mode)
                or meta.st_nlink != 1
                or meta.st_size > 65536
                or operator
                and os.name != "nt"
                and (meta.st_uid != 0 or meta.st_mode & 0o022)
            ):
                raise BootError("runtime_adoption_file_untrusted")
            raw = handle.read(65537)
        if len(raw) > 65536:
            raise BootError("runtime_adoption_file_untrusted")

        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("duplicate")
                result[key] = value
            return result

        value = json.loads(raw, object_pairs_hook=unique)
        if not isinstance(value, dict):
            raise ValueError("object")
        return value, hashlib.sha256(raw).hexdigest()
    except (OSError, ValueError, TypeError):
        raise BootError("runtime_adoption_file_untrusted") from None


def _identity(value):
    if not isinstance(value, dict) or set(value) != {
        "intent_id",
        "instance_id",
        "configuration_id",
        "sources",
    }:
        return False
    sources = value["sources"]
    return (
        all(
            isinstance(value[field], str) and UUID.fullmatch(value[field])
            for field in ("intent_id", "instance_id")
        )
        and isinstance(value["configuration_id"], str)
        and re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", value["configuration_id"])
        is not None
        and isinstance(sources, dict)
        and set(sources) == {"bootstrap_cloud.py", "model_manifest.json"}
        and all(
            isinstance(digest, str) and HASH.fullmatch(digest)
            for digest in sources.values()
        )
    )


def load_adoption(path, *, finite, intent, sources, now, receipt=None):
    """Read a bounded root authorization and bind it to this exact paid pod.

    A past authorization may only reconnect a previously adopted identity-bound
    receipt. It never creates a new adoption after its expiry or extends a lease.
    """
    value, digest = _read(path, operator=True)
    expected = {
        "intent_id": intent["id"],
        "instance_id": intent["provider_instance_id"],
        "configuration_id": finite.configuration_id,
        "sources": sources,
    }
    old, new = value.get("old_identity"), value.get("new_identity")
    if (
        set(value) != FIELDS
        or type(value.get("version")) is not int
        or value["version"] != 1
        or value.get("profile") != QUEUED_TASK_PROFILE
        or finite.qualification_profile != QUEUED_TASK_PROFILE
        or not _identity(old)
        or not _identity(new)
        or new != expected
        or any(
            old[field] != new[field]
            for field in ("intent_id", "instance_id", "sources")
        )
        or old["configuration_id"] == new["configuration_id"]
        or value.get("model_id") != MODEL
        or value.get("model_revision") != MODEL_REVISION
        or value.get("comfyui_revision") != COMFY_REVISION
        or value.get("operator_config_hash") != finite.fingerprint()
        or not isinstance(value.get("ledger_handoff_sha256"), str)
        or not HASH.fullmatch(value["ledger_handoff_sha256"])
        or not isinstance(value.get("physical_gpu_uuid"), str)
        or re.fullmatch(r"GPU-[A-Za-z0-9-]{8,100}", value["physical_gpu_uuid"]) is None
    ):
        raise BootError("runtime_adoption_identity_unconfirmed")
    issued, expiry = value["issued_at"], value["expires_at"]
    if any(
        type(t) not in (int, float) or not math.isfinite(t)
        for t in (issued, expiry, now)
    ) or not 0 < issued < expiry <= min(finite.hard_deadline, intent["hard_deadline"]):
        raise BootError("runtime_adoption_window_invalid")
    existing = (receipt or {}).get("runtime_adoption")
    if existing is not None:
        if (
            not isinstance(existing, dict)
            or set(existing)
            != {
                "proof_sha256",
                "adopted_at",
                "old_identity_hash",
                "generation_verified",
            }
            or existing.get("proof_sha256") != digest
            or existing.get("old_identity_hash") != request_hash(old)
            or existing.get("generation_verified") is not False
            or type(existing.get("adopted_at")) not in (int, float)
            or not issued <= existing["adopted_at"] < expiry
            or now < existing["adopted_at"]
            or receipt.get("identity") != expected
            or receipt.get("qualification_profile") != QUEUED_TASK_PROFILE
            or receipt.get("phase")
            not in {"booting", "runtime_ready", "fleet_starting", "fleet_started"}
        ):
            raise BootError("runtime_adoption_receipt_unconfirmed")
    elif not issued <= now < expiry:
        raise BootError("runtime_adoption_authorization_expired")
    tasks = value["prior_tasks"]
    tag = "boot-" + intent["id"].replace("-", "")
    allowed = {tag, "firstlast4-768p-5s-v1-" + tag, "ref4-bounded-768p-5s-v1-" + tag}
    if not isinstance(tasks, list) or not 1 <= len(tasks) <= 3:
        raise BootError("runtime_adoption_prior_tasks_unconfirmed")
    seen_tags, seen_tasks = set(), set()
    for task in tasks:
        if (
            not isinstance(task, dict)
            or set(task) != {"tag", "task_id", "status", "upstream_stopped"}
            or not isinstance(task.get("tag"), str)
            or task["tag"] not in allowed
            or task["tag"] in seen_tags
            or not isinstance(task.get("task_id"), str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", task["task_id"])
            or task["task_id"] in seen_tasks
            or task.get("status") not in {"succeeded", "failed", "cancelled"}
            or task.get("upstream_stopped") is not True
        ):
            raise BootError("runtime_adoption_prior_tasks_unconfirmed")
        seen_tags.add(task["tag"])
        seen_tasks.add(task["task_id"])
    return value, digest


class AdoptedHost:
    """Expose a new controller identity only after checking its real old marker."""

    def __init__(self, host, proof):
        self.host, self.proof = host, proof

    def __getattr__(self, name):
        return getattr(self.host, name)

    def report(self):
        report = self.host.report()
        if (
            not isinstance(report, dict)
            or report.get("identity") != self.proof["old_identity"]
        ):
            raise BootError("runtime_adoption_remote_identity_unconfirmed")
        gpus = report.get("gpus")
        if (
            report.get("state") != "ready"
            or not isinstance(gpus, list)
            or len(gpus) != 1
            or not isinstance(gpus[0], dict)
            or gpus[0].get("uuid") != self.proof["physical_gpu_uuid"]
        ):
            raise BootError("runtime_adoption_physical_runtime_unconfirmed")
        mapped = copy.deepcopy(report)
        mapped["identity"] = copy.deepcopy(self.proof["new_identity"])
        return mapped

    def upload(self, *args, **kwargs):
        raise BootError("runtime_adoption_cannot_restart_setup")

    def start(self, *args, **kwargs):
        raise BootError("runtime_adoption_cannot_restart_setup")


def prepare_adoption(boot, intent):
    """Prepare a fresh boot receipt or reconnect its already adopted runtime.

    Returns a safe waiting state, or None to continue the normal queued-task
    runtime/fleet checks. No root proof means the original strict boot path.
    """
    directory = boot.config.work_dir / intent["id"]
    proof_path, receipt_path = (
        directory / "runtime-adoption.json",
        directory / "bootstrap-state.json",
    )
    if not proof_path.exists() and not proof_path.is_symlink():
        return None
    files, manifest = boot._sources()
    sources = {name: hashlib.sha256(raw).hexdigest() for name, raw in files.items()}
    state = (
        _read(receipt_path)[0]
        if receipt_path.exists() or receipt_path.is_symlink()
        else None
    )
    proof, digest = load_adoption(
        proof_path,
        finite=boot.finite,
        intent=intent,
        sources=sources,
        now=boot.repo.clock(),
        receipt=state,
    )
    if state is not None and state.get("runtime_adoption") is None:
        raise BootError("runtime_adoption_requires_fresh_boot_receipt")
    if boot.host is None:
        coordinates = boot.provider.ssh_connection(
            intent["id"], intent["provider_instance_id"]
        )
        boot.host = AdoptedHost(boot.ssh_factory(boot.config, coordinates), proof)
    elif not isinstance(boot.host, AdoptedHost) or boot.host.proof != proof:
        raise BootError("runtime_adoption_host_binding_unconfirmed")
    report = boot.host.report()
    boot._validate_report(report, manifest)
    boot.host.open_tunnel(boot.config.local_port)
    if boot.backend is None:
        endpoint = f"http://127.0.0.1:{boot.config.local_port}"
        boot.backend = boot.backend_factory(
            endpoint=endpoint,
            enabled=True,
            allowed_origins=(endpoint,),
            comfy_revision=COMFY_REVISION,
        )
    if state is not None:
        # Fleet-owned real jobs may now be busy; only their existing durable
        # attempts decide whether to collect/reconcile. Never re-admit by queue.
        return None
    for task in proof["prior_tasks"]:
        result = boot.backend.poll(task["tag"], task["task_id"])
        if (
            result.state not in {"succeeded", "failed", "cancelled"}
            or result.task_id != task["task_id"]
        ):
            return {
                "state": "runtime_adoption_waiting_prior_tasks",
                "generation_verified": False,
            }
        if result.state != task["status"]:
            raise BootError("runtime_adoption_prior_terminal_status_changed")
    queue = boot.backend._json("GET", "/queue")
    if (
        not isinstance(queue, dict)
        or queue.get("queue_running") != []
        or queue.get("queue_pending") != []
    ):
        return {"state": "runtime_adoption_upstream_busy", "generation_verified": False}
    directory.mkdir(parents=True, exist_ok=True)
    state = {
        "identity": proof["new_identity"],
        "phase": "booting",
        "tag": "boot-" + intent["id"].replace("-", ""),
        "created_at": boot.repo.clock(),
        "local_port": boot.config.local_port,
        "qualification_profile": QUEUED_TASK_PROFILE,
        "runtime_adoption": {
            "proof_sha256": digest,
            "adopted_at": boot.repo.clock(),
            "old_identity_hash": request_hash(proof["old_identity"]),
            "generation_verified": False,
        },
    }
    boot._save(receipt_path, state)
    return None
