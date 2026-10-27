"""Small identity-bound startup failures; stdlib only, never runtime ownership proof."""

import json
import math
import os
from pathlib import Path
import re
import stat
import tempfile
import time

PHASES = frozenset(
    {
        "runtime_imports",
        "runtime_manifest",
        "runtime_token",
        "runtime_journal",
        "runtime_inputs",
        "runtime_host",
        "runtime_verification",
        "runtime_session_initialization",
        "runtime_http_service",
    }
)
TYPES = frozenset(
    {
        "ValueError",
        "TypeError",
        "OSError",
        "FileNotFoundError",
        "PermissionError",
        "RuntimeError",
        "ImportError",
        "ModuleNotFoundError",
        "JSONDecodeError",
        "BackendError",
        "NotReady",
    }
)
CODES = frozenset(
    {
        "wangp_runtime_dependency_missing",
        "wangp_startup_stage_failed",
        "wangp_media_tools_missing",
        "wangp_media_tools_probe_failed",
        "wangp_media_tools_probe_timeout",
        "wangp_media_tools_version_unconfirmed",
        "wangp_media_tools_version_unsupported",
        "wangp_media_tools_fps_mode_unsupported",
        "wangp_media_tools_output_invalid",
        "wangp_media_tools_binary_invalid",
        "wangp_media_tools_binary_changed",
        "wangp_media_tools_invalid",
        "wangp_synthetic_manifest_forbidden",
        "wangp_verified_manifest_changed",
        "wangp_token_permissions",
        "wangp_invalid_token",
        "wangp_invalid_token_file",
        "wangp_configuration_permissions",
        "wangp_absolute_configuration_required",
        "wangp_configuration_limit",
        "wangp_configuration_object_required",
        "wangp_unsafe_directory",
        "wangp_unsafe_file",
        "wangp_directory_missing",
        "wangp_file_replaced",
        "wangp_file_changed",
        "wangp_journal_missing",
        "wangp_journal_identity_mismatch",
        "wangp_journal_invalid",
        "wangp_journal_unavailable",
        "wangp_journal_replaced",
        "wangp_host_slot_owned",
        "wangp_runtime_config_mismatch",
        "wangp_runtime_model_root_mismatch",
        "wangp_runtime_source_mismatch",
        "wangp_runtime_source_modified",
        "wangp_runtime_requirements_mismatch",
        "wangp_runtime_untracked_code",
        "wangp_runtime_dependency_mismatch",
        "wangp_component_size_mismatch",
        "wangp_component_hash_mismatch",
        "wangp_profile_manifest_mismatch",
        "wangp_runtime_import_collision",
        "wangp_profile_one_visible_gpu_required",
        "wangp_profile_gpu_mismatch",
        "wangp_profile_memory_headroom_insufficient",
        "wangp_profile_memory_headroom_insufficient_ram_total",
        "wangp_profile_memory_headroom_insufficient_ram_available",
        "wangp_profile_memory_headroom_insufficient_vram_free",
        "wangp_profile_cgroup_invalid",
        "wangp_profile_effective_config_changed",
        "wangp_profile_effective_backend_changed",
        "wangp_profile_model_definition_changed",
    }
)


def _identity(slot_key, manifest_digest, launch_id, pid):
    if (
        not isinstance(slot_key, str)
        or not re.fullmatch(r"[A-Za-z0-9_.-]{1,200}", slot_key)
        or slot_key in {".", ".."}
        or not isinstance(manifest_digest, str)
        or not re.fullmatch(r"[0-9a-f]{64}", manifest_digest)
        or not isinstance(launch_id, str)
        or not re.fullmatch(r"[0-9a-f]{32}", launch_id)
        or type(pid) is not int
        or pid <= 0
    ):
        raise ValueError("startup_receipt_identity_invalid")
    return {
        "slot_key": slot_key,
        "manifest_digest": manifest_digest,
        "launch_id": launch_id,
        "pid": pid,
    }


def _path(path):
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("startup_receipt_path_invalid")
    for parent in path.parents:
        info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise ValueError("startup_receipt_path_invalid")
    if path.is_symlink():
        raise ValueError("startup_receipt_path_invalid")
    return path


def write_startup_failure(
    path, *, slot_key, manifest_digest, launch_id, phase, error, pid=None
):
    """Publish once; never store exception text, traceback, paths, settings or logs."""
    identity = _identity(
        slot_key, manifest_digest, launch_id, os.getpid() if pid is None else pid
    )
    path = _path(path)
    if phase not in PHASES:
        raise ValueError("startup_receipt_phase_invalid")
    code = (
        "wangp_runtime_dependency_missing"
        if isinstance(error, ModuleNotFoundError)
        else "wangp_startup_stage_failed"
    )
    if isinstance(error, Exception) and str(error) in CODES:
        code = str(error)
    value = {
        "schema_version": 1,
        "state": "runtime_start_failed",
        **identity,
        "phase": phase,
        "error_code": code,
        "error_type": type(error).__name__
        if type(error).__name__ in TYPES
        else "RuntimeStartupError",
        "observed_at": time.time(),
        "inference_verified": False,
    }
    descriptor, name = tempfile.mkstemp(
        prefix=".startup-", suffix=".tmp", dir=path.parent
    )
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as target:
            json.dump(value, target, sort_keys=True, allow_nan=False)
            target.flush()
            os.fsync(target.fileno())
        # Atomic exclusive publication: a prior failure receipt is never replaced.
        os.link(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return value


def read_startup_failure(path, *, slot_key, manifest_digest, launch_id, pid):
    identity = _identity(slot_key, manifest_digest, launch_id, pid)
    path = _path(path)
    info = path.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or info.st_size > 8192
        or os.name != "nt"
        and info.st_mode & 0o077
    ):
        raise ValueError("startup_receipt_invalid")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "r", encoding="utf-8") as source:
        opened = os.fstat(source.fileno())
        if (info.st_dev, info.st_ino) != (opened.st_dev, opened.st_ino):
            raise ValueError("startup_receipt_invalid")
        value = json.loads(source.read(8193))
    expected = {
        "schema_version",
        "state",
        *identity,
        "phase",
        "error_code",
        "error_type",
        "observed_at",
        "inference_verified",
    }
    if (
        not isinstance(value, dict)
        or set(value) != expected
        or type(value["schema_version"]) is not int
        or value["schema_version"] != 1
        or value["state"] != "runtime_start_failed"
        or any(value[k] != v for k, v in identity.items())
        or type(value["pid"]) is not int
        or value["phase"] not in PHASES
        or value["error_code"] not in CODES
        or value["error_type"] not in TYPES | {"RuntimeStartupError"}
        or value["inference_verified"] is not False
        or type(value["observed_at"]) not in (int, float)
        or not math.isfinite(value["observed_at"])
        or value["observed_at"] <= 0
    ):
        raise ValueError("startup_receipt_invalid")
    return value
