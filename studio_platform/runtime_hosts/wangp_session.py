"""Explicit-start facade for the pinned upstream headless Session API.

Importing this module does not import WanGP/torch, load weights, or start threads.
The HTTP host owns durable receipts; this facade owns one live runtime handle.
"""

from __future__ import annotations

import copy
import hashlib
import importlib
import importlib.metadata
import json
import os
import stat
from pathlib import Path
import re
import subprocess
import sys
import threading
import time

from ..inference.protocol import BackendError, NotReady
from ..inference.wangp_contract import (
    EngineManifest,
    RuntimeObservation,
    RuntimeOutput,
    UPSTREAM_REVISION,
)

REQUIRED_CONFIG = {
    "transformer_quantization": "bf16",
    "text_encoder_quantization": "bf16",
    "transformer_dtype_policy": "bf16",
    "attention_mode": "sdpa",
    "video_profile": 4,
    "profile": 4,
    "vae_config": 3,
    "vae_precision": "16",
    "mixed_precision": "0",
    "compile": "",
    "boost": 1,
    "int8_kernels": "disabled",
    "kernel_precision": "strict",
    "fit_canvas": 2,
    "video_output_codec": "libx264_8",
    "video_container": "mp4",
    "audio_output_codec": "aac_128",
    "enhancer_enabled": 0,
    "save_queue_if_crash": 0,
    "notification_sound_enabled": 0,
    "embed_source_images": False,
}
CORE_VERSIONS = {
    "torch": "2.10.0+cu128",
    "torchvision": "0.25.0+cu128",
    "torchaudio": "2.10.0+cu128",
    "diffusers": "0.36.0",
    "transformers": "4.54.0",
    "numpy": "2.1.2",
    "optimum-quanto": "0.2.7",
    "comfy-kitchen": "0.2.35",
}


def _contained_file(root: Path, path: Path) -> Path:
    """Reject links and escaping paths; output names never come from clients."""
    root = root.resolve(strict=True)
    candidate = path if path.is_absolute() else root / path
    # resolve alone would hide a symlink that points back inside the root.
    try:
        relative = candidate.absolute().relative_to(root)
    except ValueError:
        raise BackendError("wangp_output_path_invalid") from None
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise BackendError("wangp_output_path_invalid")
    target = candidate.resolve(strict=True)
    if not target.is_relative_to(root) or not target.is_file():
        raise BackendError("wangp_output_path_invalid")
    return target


def _check_config(path, model_root=None, deployment_profile_id=None):
    path = Path(path).resolve(strict=True)
    if path.name != "wgp_config.json":
        raise ValueError("wangp_config_filename_invalid")
    data = json.loads(path.read_text(encoding="utf-8"))
    required = REQUIRED_CONFIG
    if deployment_profile_id is not None:
        from ..runtime_catalog import get_profile

        required = get_profile(deployment_profile_id)['runtime']['config']
    if (
        not isinstance(data, dict)
        or set(data) != set(required) | {"checkpoints_paths"}
        or any(
            json.dumps(data.get(key), sort_keys=True)
            != json.dumps(value, sort_keys=True)
            for key, value in required.items()
        )
    ):
        raise ValueError("wangp_runtime_config_mismatch")
    paths = data.get("checkpoints_paths")
    if (
        not isinstance(paths, list)
        or len(paths) != 1
        or not isinstance(paths[0], str)
        or not Path(paths[0]).is_absolute()
    ):
        raise ValueError("wangp_runtime_model_root_mismatch")
    actual = Path(paths[0]).resolve(strict=True)
    if model_root is not None and actual != Path(model_root).resolve(strict=True):
        raise ValueError("wangp_runtime_model_root_mismatch")
    return data


def verify_runtime(runtime_root, config_path, manifest_path, model_root):
    # fmt: off
    """Explicit local attestation, not a model test and never an installer.

Hashing weights occurs only when this function is explicitly called on the GPU
host. No historical manifest/filename/size is treated as current verification.
The returned evidence contains no runtime environment or configuration values.
"""
    # fmt: on
    root = Path(runtime_root).resolve(strict=True)
    models = Path(model_root).resolve(strict=True)
    manifest = EngineManifest.from_dict(
        json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    )
    profile = None
    if manifest.document.get('deployment_profile_id') is not None:
        from ..runtime_catalog import validate_manifest

        profile = validate_manifest(manifest)
    config = _check_config(config_path, models, profile['id'] if profile else None)
    environment_evidence = {}
    if (
        manifest.document.get("runtime_digest_kind")
        == "sixnine-environment-lock-sha256"
    ):
        from .wangp_environment import verify_bound_environment

        environment_evidence = verify_bound_environment(root, manifest.document)
        revision = UPSTREAM_REVISION
    else:
        # Historical candidate manifests retain their exact verification path;
        # new cold bootstrap requires the fully bound package above.
        revision = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        if revision != UPSTREAM_REVISION:
            raise ValueError("wangp_runtime_source_mismatch")
        # Pilot image modified installation metadata only; executable source and
        # the exact requirements bytes remain pinned for the new native profiles.
        extra = [":(exclude)requirements.txt"] if profile else []
        for args in (
            ["diff", "--quiet", "HEAD", "--", *extra],
            ["diff", "--cached", "--quiet", "HEAD", "--", *extra],
        ):
            if subprocess.run(
                ["git", "-C", str(root), *args], capture_output=True
            ).returncode:
                raise ValueError("wangp_runtime_source_modified")
        if (
            profile
            and hashlib.sha256((root / 'requirements.txt').read_bytes()).hexdigest()
            != profile['runtime']['requirements_sha256']
        ):
            raise ValueError('wangp_runtime_requirements_mismatch')
        untracked = (
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(root),
                    "ls-files",
                    "--others",
                    "--exclude-standard",
                    "-z",
                ],
                check=True,
                capture_output=True,
            )
            .stdout.decode()
            .split("\0")
        )
        if any(
            Path(name).suffix.lower() in {".py", ".pyd", ".so"}
            for name in untracked
            if name
        ):
            raise ValueError("wangp_runtime_untracked_code")
    versions = {}
    for package, required in (
        profile['runtime']['core_versions'] if profile else CORE_VERSIONS
    ).items():
        version = importlib.metadata.version(package)
        if version != required:
            raise ValueError("wangp_runtime_dependency_mismatch")
        versions[package] = version
    checked = []
    for component in manifest.document["components"].values():
        for record in component["files"]:
            path = _contained_file(models, Path(record["path"]))
            if path.stat().st_size != record["size_bytes"]:
                raise ValueError("wangp_component_size_mismatch")
            sha = hashlib.sha256()
            git_blob = hashlib.sha1(
                ("blob " + str(record["size_bytes"]) + "\0").encode()
            )
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
                    sha.update(chunk)
                    git_blob.update(chunk)
            expected, observed = (
                (record["sha256"], sha.hexdigest())
                if "sha256" in record
                else (record["git_blob_sha1"], git_blob.hexdigest())
            )
            if expected != observed:
                raise ValueError("wangp_component_hash_mismatch")
            checked.append(
                {
                    "path": record["path"],
                    "sha256": sha.hexdigest(),
                    "size_bytes": record["size_bytes"],
                }
            )
    return {
        "manifest_digest": manifest.digest,
        "source_revision": revision,
        "core_versions": versions,
        "verified_files": checked,
        "config_sha256": hashlib.sha256(
            json.dumps(config, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        "inference_verified": False,
        **environment_evidence,
    }


def config_for_model_root(model_root, deployment_profile_id=None):
    """Non-secret explicit startup configuration; caller writes wgp_config.json."""
    if deployment_profile_id is not None:
        from ..runtime_catalog import runtime_config

        return runtime_config(deployment_profile_id, model_root)
    return {
        **REQUIRED_CONFIG,
        "checkpoints_paths": [str(Path(model_root).resolve(strict=True))],
    }


def _write_float_wav(path, samples, rate):
    # Imported only after a real upstream result. Preserve generated float samples;
    # the muxed MP4's AAC stream is not decoded/re-encoded to fabricate a raw WAV.
    import numpy as np
    import soundfile

    value = np.asarray(samples, dtype=np.float32)
    if (
        value.ndim != 2
        or value.shape[1] != 2
        or not value.shape[0]
        or not np.isfinite(value).all()
        or rate != 32000
    ):
        raise BackendError("wangp_audio_payload_invalid")
    soundfile.write(str(path), value, rate, format="WAV", subtype="FLOAT")


def _upstream_worker_alive():
    return any(
        thread.name == "wangp-session-worker" and thread.is_alive()
        for thread in threading.enumerate()
    )


def _lock_profile_device(torch, profile):
    """One explicit visible device and one owned process per physical GPU."""
    import fcntl
    import stat

    if not torch.cuda.is_available() or torch.cuda.device_count() != 1:
        raise ValueError('wangp_profile_one_visible_gpu_required')
    properties = torch.cuda.get_device_properties(0)
    name = torch.cuda.get_device_name(0)
    pruned = profile['model_id'] == 'MiniMax-H3-Pruned-Rank8-INT8'
    hardware = profile.get('hardware_admission')
    if hardware is not None:
        accepted = name.removeprefix('NVIDIA ') in hardware['gpu_models']
        capability = tuple(hardware['compute_capability'])
        minimum, maximum = (
            hardware['minimum_total_vram_bytes'],
            hardware['maximum_total_vram_bytes'],
        )
    else:
        accepted = (
            re.fullmatch(r'(?:NVIDIA\s+)?(?:GeForce\s+)?RTX\s+5090', name)
            if pruned
            else re.fullmatch(
                r'(?:NVIDIA\s+)?RTX\s+PRO\s+6000\s+Blackwell\s+(?:Server|Workstation)\s+Edition',
                name,
            )
        )
        capability = (12, 0)
        low, high = (30, 34) if pruned else (90, 100)
        minimum, maximum = low * 1024**3, high * 1024**3
    if (
        not accepted
        or torch.cuda.get_device_capability(0) != capability
        or not minimum <= properties.total_memory <= maximum
    ):
        raise ValueError('wangp_profile_gpu_mismatch')
    uuid = str(getattr(properties, 'uuid', ''))
    if not re.fullmatch(
        r'(?:GPU-)?[a-fA-F0-9]{8}(?:-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12}', uuid
    ):
        raise ValueError('wangp_profile_gpu_identity_required')
    uuid = uuid.removeprefix('GPU-').lower()
    fd = os.open(
        '/tmp/sixnine-wangp-gpu-' + uuid + '.lock',
        os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW,
        0o600,
    )
    stream = os.fdopen(fd, 'a+')
    try:
        info = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_nlink != 1
        ):
            raise ValueError('wangp_profile_gpu_lock_invalid')
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except Exception:
        stream.close()
        raise
    return stream


def _profile_ram_groups(proc_root, group_root, *, resolve_membership=False):
    """Resolve the complete visible v2 ancestry, never follow an escaping link."""
    if (group_root / 'memory.max').is_file() and not resolve_membership:
        # Existing container deployments expose their constrained group here.
        group = group_root
    else:
        # Capacity admission resolves the actual process even when the mount
        # root is constrained: a visible child can impose a tighter limit.
        # Older available-headroom admission retains its container-root path.
        from pathlib import PurePosixPath

        groups = [
            line[3:]
            for line in (proc_root / 'self/cgroup').read_text().splitlines()
            if line.startswith('0::')
        ]
        if (
            len(groups) != 1
            or not groups[0].startswith('/')
            or '..' in PurePosixPath(groups[0]).parts
        ):
            raise ValueError('wangp_profile_cgroup_invalid')
        if not (group_root / 'cgroup.controllers').is_file():
            raise ValueError('wangp_profile_cgroup_invalid')
        group = group_root / groups[0].lstrip('/')
        if not group.is_dir():
            raise ValueError('wangp_profile_cgroup_invalid')
    result = []
    while True:
        if group.is_symlink() or not group.is_dir():
            raise ValueError('wangp_profile_cgroup_invalid')
        result.append(group)
        if group == group_root:
            return result
        group = group.parent


def _profile_ram_limit(group):
    path = group / 'memory.max'
    if path.is_symlink():
        raise ValueError('wangp_profile_cgroup_invalid')
    try:
        value = path.read_text().strip()
    except (OSError, UnicodeError):
        raise ValueError('wangp_profile_cgroup_invalid') from None
    if value == 'max':
        return None
    if not value.isdecimal() or int(value) <= 0:
        raise ValueError('wangp_profile_cgroup_invalid')
    return int(value)


def _profile_v1_ram(proc_root, group_root):
    """Observe the actual v1 memory controller, including hybrid hosts.

    Kernel memory.stat supplies the limit of hidden ancestors. Hidden ancestor
    usage cannot be reconstructed from a leaf (siblings may consume it), so a
    subtree mount returns zero as its conservative available-memory bound.
    This never substitutes host RAM for an unobserved controller constraint.
    """
    from pathlib import PurePosixPath

    def path(value):
        # mountinfo encodes spaces, tabs, newlines and backslashes in octal.
        value = re.sub(r'\\([0-7]{3})', lambda match: chr(int(match[1], 8)), value)
        if (
            not value.startswith('/')
            or '\\' in value
            or any(ord(c) < 32 for c in value)
            or any(part in {'.', '..'} for part in value.split('/'))
        ):
            raise ValueError('wangp_profile_cgroup_invalid')
        return PurePosixPath(value)

    def read(candidate):
        try:
            info = candidate.lstat()
            if (
                not stat.S_ISREG(info.st_mode)
                or candidate.is_symlink()
                or info.st_size > 262144
            ):
                raise ValueError('wangp_profile_cgroup_invalid')
            value = candidate.read_text()
            if len(value) > 262144:
                raise ValueError('wangp_profile_cgroup_invalid')
            return value
        except (OSError, UnicodeError):
            raise ValueError('wangp_profile_cgroup_invalid') from None

    def number(value, *, positive=False):
        if not value.isdecimal() or positive and int(value) <= 0:
            raise ValueError('wangp_profile_cgroup_invalid')
        return int(value)

    try:
        memberships = [
            parts[2]
            for line in (proc_root / 'self/cgroup').read_text().splitlines()
            if len(parts := line.split(':', 2)) == 3 and 'memory' in parts[1].split(',')
        ]
        if not memberships:
            return None  # The existing v2 observer decides whether that is valid.
        if len(memberships) != 1:
            raise ValueError('wangp_profile_cgroup_invalid')
        # A hybrid hierarchy may mount cgroup2 for other controllers while
        # memory still belongs to v1. Never admit contradictory ownership.
        controllers = group_root / 'cgroup.controllers'
        try:
            controllers.lstat()
        except FileNotFoundError:
            pass
        else:
            if 'memory' in read(controllers).split():
                raise ValueError('wangp_profile_cgroup_invalid')
        membership = path(memberships[0])
        mounted = group_root / 'memory'
        matches = []
        for line in (proc_root / 'self/mountinfo').read_text().splitlines():
            fields, separator, filesystem = line.partition(' - ')
            before, after = fields.split(), filesystem.split()
            if (
                separator
                and len(before) >= 6
                and len(after) >= 3
                and after[0] == 'cgroup'
                and 'memory' in after[2].split(',')
            ):
                root, point = path(before[3]), path(before[4])
                if point == PurePosixPath(mounted.as_posix()):
                    matches.append(root)
        if len(matches) != 1 or not membership.is_relative_to(matches[0]):
            raise ValueError('wangp_profile_cgroup_invalid')
        mount_root = matches[0]
        group = mounted.joinpath(*membership.relative_to(mount_root).parts)
        for candidate in (mounted, *mounted.parents):
            if candidate.is_symlink() or not candidate.is_dir():
                raise ValueError('wangp_profile_cgroup_invalid')
        capacity = None
        available = None
        while True:
            if group.is_symlink() or not group.is_dir():
                raise ValueError('wangp_profile_cgroup_invalid')
            if read(group / 'memory.use_hierarchy').strip() != '1':
                raise ValueError('wangp_profile_cgroup_invalid')
            limit = number(read(group / 'memory.limit_in_bytes').strip(), positive=True)
            used = number(read(group / 'memory.usage_in_bytes').strip())
            fields = {}
            for line in read(group / 'memory.stat').splitlines():
                key, value = line.split()
                if key in fields or not re.fullmatch(r'[a-z_]+', key):
                    raise ValueError('wangp_profile_cgroup_invalid')
                fields[key] = number(value)
            hierarchy = number(
                str(fields.get('hierarchical_memory_limit', '')), positive=True
            )
            limit = min(limit, hierarchy)
            capacity = limit if capacity is None else min(capacity, limit)
            # Never mix local and hierarchical counters. If an old kernel lacks
            # a shmem counter, no cache is credited as safely reclaimable.
            keys = (
                'cache',
                'shmem',
                'active_file',
                'inactive_file',
                'dirty',
                'writeback',
                'unevictable',
            )
            reclaimable = 0
            if all('total_' + key in fields for key in keys):
                cache = max(0, fields['total_cache'] - fields['total_shmem'])
                lru = fields['total_active_file'] + fields['total_inactive_file']
                exclusions = sum(
                    fields['total_' + key]
                    for key in ('dirty', 'writeback', 'unevictable')
                )
                reclaimable = max(0, min(cache, lru) - exclusions)
            bound = min(limit, max(0, limit - used + reclaimable))
            available = bound if available is None else min(available, bound)
            if group == mounted:
                # Limits include invisible ancestors, but their consumption is
                # unobserved. Preserve older headroom profiles conservatively.
                return capacity, available if mount_root == PurePosixPath('/') else 0
            group = group.parent
    except (OSError, UnicodeError, TypeError, ValueError):
        raise ValueError('wangp_profile_cgroup_invalid') from None


def _effective_total_profile_ram(
    proc_root=Path('/proc'), group_root=Path('/sys/fs/cgroup')
):
    """Allocated capacity: host total constrained by every finite ancestor."""
    info = {
        line.split(':', 1)[0]: int(line.split()[1]) * 1024
        for line in (proc_root / 'meminfo').read_text().splitlines()
    }
    total = info.get('MemTotal')
    if type(total) is not int or total <= 0:
        raise ValueError('wangp_profile_total_ram_unconfirmed')
    legacy = _profile_v1_ram(proc_root, group_root)
    if legacy is not None:
        return min(total, legacy[0])
    for group in _profile_ram_groups(proc_root, group_root, resolve_membership=True):
        # A whole VM root can lack memory.max. A resolved non-root memory
        # membership must expose its limit; missing observation is not max.
        try:
            limit_stat = (group / 'memory.max').lstat()
        except FileNotFoundError:
            if group == group_root:
                continue
            raise ValueError('wangp_profile_cgroup_invalid')
        except OSError:
            raise ValueError('wangp_profile_cgroup_invalid') from None
        if not stat.S_ISREG(limit_stat.st_mode):
            raise ValueError('wangp_profile_cgroup_invalid')
        limit = _profile_ram_limit(group)
        if limit is not None:
            total = min(total, limit)
    return total


def _available_profile_ram(
    proc_root=Path('/proc'),
    group_root=Path('/sys/fs/cgroup'),
    *,
    resolve_membership=False,
):
    """Headroom after imports; clean mapped file-cache pages are reclaimable."""
    info = {
        line.split(':', 1)[0]: int(line.split()[1]) * 1024
        for line in (proc_root / 'meminfo').read_text().splitlines()
    }
    available = info['MemAvailable']
    legacy = _profile_v1_ram(proc_root, group_root)
    if legacy is not None:
        return min(available, legacy[1])
    for group in _profile_ram_groups(
        proc_root, group_root, resolve_membership=resolve_membership
    ):
        if resolve_membership:
            try:
                metadata = (group / 'memory.max').lstat()
            except FileNotFoundError:
                if group == group_root:
                    continue
                raise ValueError('wangp_profile_cgroup_invalid')
            if not stat.S_ISREG(metadata.st_mode):
                raise ValueError('wangp_profile_cgroup_invalid')
            for name in ('memory.current', 'memory.stat'):
                path = group / name
                metadata = path.lstat()
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 262144:
                    raise ValueError('wangp_profile_cgroup_invalid')
        if (group / 'memory.max').is_file():
            limit = _profile_ram_limit(group)
            used = int((group / 'memory.current').read_text())
            fields = dict(
                (key, int(value))
                for key, value in (
                    line.split()
                    for line in (group / 'memory.stat').read_text().splitlines()
                )
            )
            if resolve_membership and (
                used < 0 or any(value < 0 for value in fields.values())
            ):
                raise ValueError('wangp_profile_cgroup_invalid')
            file_bytes = max(0, fields.get('file', 0) - fields.get('shmem', 0))
            lru = max(0, fields.get('active_file', 0) + fields.get('inactive_file', 0))
            exclusions = sum(
                max(0, fields.get(k, 0))
                for k in ('file_dirty', 'file_writeback', 'unevictable')
            )
            reclaimable = max(0, min(file_bytes, lru) - exclusions)
            if limit is not None:
                available = min(available, limit, max(0, limit - used + reclaimable))
    return available


def _profile_memory_admission(torch, profile):
    """Keep allocation evidence separate from an older headroom contract."""
    free, _ = torch.cuda.mem_get_info(0)
    runtime = profile['runtime']
    metric = runtime.get('ram_admission_metric', 'available-headroom-v1')
    available = (
        _available_profile_ram(resolve_membership=True)
        if metric == 'effective-total-and-available-v1'
        else _available_profile_ram()
    )
    if metric in {'effective-total-v1', 'effective-total-and-available-v1'}:
        capacity = _effective_total_profile_ram()
        minimum = runtime['minimum_total_ram_bytes']
        if type(minimum) is not int or minimum <= 0:
            raise ValueError('wangp_profile_total_ram_unconfirmed')
        observation = {
            'event': 'profile_memory_admission',
            'ram_metric': metric,
            'effective_total_ram_bytes': capacity,
            'available_ram_bytes': available,
            'minimum_total_ram_bytes': minimum,
            'free_vram_bytes': free,
        }
        ram_insufficient = capacity < minimum
        ram_reason = 'ram_total'
        if metric == 'effective-total-and-available-v1':
            headroom = runtime['minimum_available_ram_bytes']
            if type(headroom) is not int or headroom <= 0:
                raise ValueError('wangp_profile_available_ram_unconfirmed')
            observation['minimum_available_ram_bytes'] = headroom
            if not ram_insufficient:
                ram_insufficient = available < headroom
                ram_reason = 'ram_available'
        print(json.dumps(observation), flush=True)
    elif metric == 'available-headroom-v1':
        ram_insufficient = available < runtime['minimum_available_ram_bytes']
        ram_reason = 'ram_available'
    else:
        raise ValueError('wangp_profile_memory_admission_metric_invalid')
    if ram_insufficient:
        raise ValueError('wangp_profile_memory_headroom_insufficient_' + ram_reason)
    if free < profile['minimum_free_vram_bytes']:
        raise ValueError('wangp_profile_memory_headroom_insufficient_vram_free')


def _audit_profile_runtime(session, manifest, requested, *, loaded=False):
    """Attest actual selection and BF16 QKV layout; no config-string fallback."""
    from ..runtime_catalog import validate_manifest, model_for
    from ..inference.wangp_contract import canonical_json

    profile = validate_manifest(manifest)
    doc, runtime = manifest.document, profile['runtime']
    module = session._ensure_runtime().module
    if any(
        canonical_json(module.server_config.get(k)) != canonical_json(v)
        for k, v in requested.items()
    ):
        raise ValueError('wangp_profile_effective_config_changed')
    if (
        module.transformer_quantization != requested['transformer_quantization']
        or module.text_encoder_quantization != requested['text_encoder_quantization']
        or module.default_profile_video != runtime['memory_profile']
        or getattr(getattr(module, 'int8_backend', None), '_backend', None)
        != runtime['effective_int8_backend']
    ):
        raise ValueError('wangp_profile_effective_backend_changed')
    model = model_for(profile['id'], doc['mode'])['model_type']
    definition = session.get_model_def(model)
    if not isinstance(definition, dict) or definition.get('architecture') != model:
        raise ValueError('wangp_profile_model_definition_changed')
    effective = definition.copy()
    groups = module.get_model_config_groups(model, definition)
    for _, _, settings in module.model_config_groups.selected_model_configs(
        groups, runtime['task_config']
    ):
        effective.update(settings)
    components = doc['components']
    transformer = components['transformer']['files'][0]['path']
    encoder = components['text_encoder']['files'][0]['path']
    selected = module.get_model_filename(
        model,
        quantization=requested['transformer_quantization'],
        dtype_policy='bf16',
        model_def=effective,
    )
    selected_encoder = module.get_model_filename(
        model,
        quantization=requested['text_encoder_quantization'],
        dtype_policy='bf16',
        URLs=effective.get('text_encoder_URLs', []),
    )
    if (
        not str(selected).endswith('/' + transformer)
        or not str(selected_encoder).endswith('/' + encoder)
        or effective.get('video_vae_file')
        != components['video_vae']['files'][0]['path']
        or effective.get('audio_vae_file', components['audio_vae']['files'][0]['path'])
        != components['audio_vae']['files'][0]['path']
        or effective.get('qkv_splitting') is not runtime['qkv_splitting']
        or any(effective.get(k) for k in ('pdd', 'vdn', 'auto_quantize'))
    ):
        raise ValueError('wangp_profile_component_selection_changed')
    root = Path(requested['checkpoints_paths'][0]).resolve(strict=True)
    resolved = {
        transformer: module.fl.get_local_model_filename(selected),
        encoder: module.fl.get_local_model_filename(
            selected_encoder, extra_paths='Qwen3-VL-32B-Instruct'
        ),
    }
    for component in components.values():
        for record in component['files']:
            name = record['path']
            if name not in resolved:
                resolved[name] = module.fl.locate_file(name)
    for name, path in resolved.items():
        if path is None or Path(path).resolve(strict=True) != _contained_file(
            root, Path(name)
        ):
            raise ValueError('wangp_profile_resolved_component_changed')
    if loaded:
        if (
            module.loaded_profile != runtime['memory_profile']
            or module.loaded_config != runtime['task_config']
            or module.transformer_type != model
        ):
            raise ValueError('wangp_profile_loaded_config_changed')
        transformer_object = module.wan_model.transformer
        attention = transformer_object.blocks[0].attn
        split = runtime['qkv_splitting']
        if (
            bool(transformer_object.split_linear_modules_map) != split
            or all(hasattr(attention, k) for k in ('q_proj', 'k_proj', 'v_proj'))
            != split
            or hasattr(attention, 'qkv_proj') == split
        ):
            raise ValueError('wangp_profile_loaded_qkv_changed')
        if profile['model_id'] != 'MiniMax-H3-Pruned-Rank8-INT8':
            checkpoint = transformer_object.h3_checkpoint_info
            if (
                checkpoint.get('compressed_modulation') is not False
                or checkpoint.get('time_embed_dim') != 2688
            ):
                raise ValueError('wangp_profile_loaded_unpruned_mismatch')


def _failure_code(errors):
    # fmt: off
    """Reduce the first upstream error to a static stage/signature label.

Pinned GenerationError exposes message/stage, not the original exception class.
Messages are examined only in memory, with a bounded prefix, and never returned
or persisted. Unknown shapes/text keep an explicit unclassified diagnostic.
"""
    # fmt: on
    stage, message = "unknown", ""
    try:
        if type(errors) in (list, tuple) and errors:
            error = errors[0]
            candidate = getattr(error, "stage", None)
            if type(candidate) is str and candidate in {
                "validation",
                "generation",
                "runtime",
            }:
                stage = candidate
            candidate = getattr(error, "message", None)
            if type(candidate) is str:
                message = candidate[:4096].lower()
    except Exception:
        pass  # Malformed diagnostics cannot change a positively stopped result.
    category = "unclassified"
    if "cuda out of memory" in message or "cuda error: out of memory" in message:
        category = "cuda_out_of_memory"
    elif any(
        signature in message
        for signature in (
            "sizes of tensors must match",
            "the size of tensor a",
            "mat1 and mat2 shapes cannot be multiplied",
        )
    ):
        category = "tensor_shape_mismatch"
    elif "invalid data found when processing input" in message:
        category = "media_decode_failed"
    elif "no module named" in message:
        category = "dependency_missing"
    elif "seed must be between 0 and 2**32 - 1" in message:
        category = "seed_out_of_range"
    return f"wangp_{stage}_{category}"


class _SessionHandle:
    def __init__(
        self, job, output_root, audio_writer, worker_alive, quiesce, result_audit=None
    ):
        self.job, self.root, self.audio_writer = job, output_root, audio_writer
        self.worker_alive, self.quiesce = worker_alive, quiesce
        self.result_audit = result_audit
        self._stop_verified = False
        self._observation = None
        self._lock = threading.Lock()

    def cancel(self):
        # Upstream returns None: accepting intent is not evidence of GPU stop.
        self.job.cancel()
        return True

    def observe(self):
        with self._lock:
            if self._observation is not None:
                return self._observation
            if self.job.done is not True:
                return RuntimeObservation("running", stopped=False)
            # Upstream's outer exception handler can publish a result while its
            # daemon generation thread remains alive. Done alone is insufficient.
            if self.worker_alive() is not False:
                return RuntimeObservation("unknown", stopped=False)
            if not self._stop_verified:
                self.quiesce()  # Actual factory supplies CUDA synchronize.
                self._stop_verified = True
            result = self.job.result(timeout=0)
            if result.cancelled:
                self._observation = RuntimeObservation("cancelled", stopped=True)
                return self._observation
            if result.success is not True:
                self._observation = RuntimeObservation(
                    "failed",
                    stopped=True,
                    error_code=_failure_code(getattr(result, "errors", None)),
                )
                return self._observation
            if (
                result.total_tasks != 1
                or result.successful_tasks != 1
                or result.failed_tasks != 0
                or result.errors
                or len(result.generated_files) != 1
            ):
                raise BackendError("wangp_result_shape_invalid")
            if self.result_audit is not None:
                self.result_audit()
            video = _contained_file(self.root, Path(result.generated_files[0]))
            if video.suffix.lower() != ".mp4" or video.stat().st_size <= 0:
                raise BackendError("wangp_video_output_missing")
            artifacts = [
                item
                for item in result.artifacts
                if item.media_type == "video"
                and item.path
                and Path(item.path).resolve() == video
            ]
            if (
                len(artifacts) != 1
                or artifacts[0].audio_tensor is None
                or artifacts[0].audio_sampling_rate != 32000
            ):
                raise BackendError("wangp_audio_output_missing")
            # Repeat observation retries packaging the SAME completed result only.
            # A collection failure never calls submit_task again.
            wav = video.with_name(video.stem + "-generated.wav")
            temp = wav.with_suffix(".pending.wav")
            if wav.is_symlink() or temp.is_symlink():
                raise BackendError("wangp_output_path_invalid")
            self.audio_writer(
                temp, artifacts[0].audio_tensor, artifacts[0].audio_sampling_rate
            )
            if not temp.is_file() or temp.stat().st_size <= 0:
                raise BackendError("wangp_audio_output_missing")
            with temp.open("r+b") as stream:
                os.fsync(stream.fileno())
            os.replace(temp, wav)
            self._observation = RuntimeObservation(
                "succeeded",
                stopped=True,
                outputs={
                    "video": RuntimeOutput(video, "video/mp4"),
                    "audio": RuntimeOutput(wav, "audio/wav"),
                },
            )
            return self._observation


class ModelLoadProgress:
    """Observe pinned upstream progress callbacks, never trigger a model load.

    Payloads may contain private prompts/status. Inspect only their enum phase;
    retain no event object, text, paths, task IDs or user settings.
    """

    LOADED_PHASES = frozenset(
        {
            "encoding_text",
            "inference",
            "inference_stage_1",
            "inference_stage_2",
            "inference_stage_3",
            "decoding",
            "downloading_output",
        }
    )

    def __init__(self, load_probe, *, clock=time.time):
        self.load_probe, self.clock = load_probe, clock
        self._lock = threading.Lock()
        now = clock()
        initial = self._probe()
        self._value = {"state": initial, "phase_started_at": now, "updated_at": now}

    def _probe(self):
        try:
            result = self.load_probe()
            return (
                result
                if type(result) is str and result in {"not_loaded", "loaded"}
                else "not_observed"
            )
        except Exception:
            return "not_observed"

    def _set(self, state, reason=None):
        now = max(self.clock(), self._value["updated_at"])
        if state != self._value["state"]:
            self._value["phase_started_at"] = now
        self._value.update(state=state, updated_at=now)
        self._value.pop("reason_code", None)
        if reason:
            self._value["reason_code"] = reason

    def on_progress(self, event):
        # Callback observation must never disrupt an accepted generation.
        try:
            phase = getattr(event, "phase", None)
            with self._lock:
                if phase == "loading_model":
                    self._set("loading")
                elif (
                    type(phase) is str
                    and phase in self.LOADED_PHASES
                    and self._probe() == "loaded"
                ):
                    self._set("loaded")
        except Exception:
            pass

    def on_status(self, value):
        # Pinned wgp.py emits this before load_models(), before its first
        # block/phase progress callback. The model-name suffix is never retained.
        try:
            if type(value) is str and value[:13].lower() == "loading model":
                with self._lock:
                    self._set("loading")
        except Exception:
            pass

    def on_error(self, _event):
        try:
            with self._lock:
                if self._value["state"] == "loading":
                    self._set(self._probe(), "wangp_model_load_failed")
        except Exception:
            pass

    def on_complete(self, _event):
        try:
            with self._lock:
                state = self._probe()
                if state == "loaded":
                    self._set(state)
        except Exception:
            pass

    def snapshot(self):
        with self._lock:
            return dict(self._value)


class PinnedWanGPSession:
    def __init__(
        self,
        session,
        output_root,
        *,
        quiesce,
        audio_writer=_write_float_wav,
        worker_alive=_upstream_worker_alive,
        manifest=None,
        before_submit=None,
        result_audit=None,
        device_lock=None,
        model_progress=None,
    ):
        if not callable(quiesce) or not callable(worker_alive):
            raise ValueError("wangp_runtime_stop_probe_required")
        self.session = session
        self.output_root = Path(output_root).resolve(strict=True)
        self.audio_writer = audio_writer
        self.worker_alive, self.quiesce = worker_alive, quiesce
        self.manifest, self.before_submit, self.result_audit = (
            manifest,
            before_submit,
            result_audit,
        )
        self.device_lock = device_lock
        self._model_progress = model_progress
        if manifest is not None:
            from ..runtime_catalog import validate_manifest

            validate_manifest(manifest)
        self._lifecycle_lock = threading.RLock()
        self._closed = False

    def is_idle(self):
        return (
            not self._closed
            and self.session.active_job is None
            and self.worker_alive() is False
        )

    def preparation(self):
        return (
            {"model": self._model_progress.snapshot()}
            if self._model_progress is not None
            else None
        )

    def submit_task(self, settings):
        with self._lifecycle_lock:
            if not self.is_idle():
                raise NotReady("wangp_session_busy")
            # Pristine computed defaults, not mutable GUI defaults/previous-job state.
            model = settings.get("model_type", "minimax_h3_fl2va")
            allowed = {"minimax_h3_fl2va", "minimax_h3_ref2va"}
            if self.manifest is not None:
                from ..runtime_catalog import model_for

                doc = self.manifest.document
                allowed = {
                    model_for(doc['deployment_profile_id'], doc['mode'])['model_type']
                }
            if model not in allowed:
                raise ValueError("wangp_session_model_unsupported")
            if self.before_submit is not None:
                self.before_submit()
            values = self.session.get_default_settings(model)
            values.update(copy.deepcopy(dict(settings)))
            job = (
                self.session.submit_task(values, callbacks=self._model_progress)
                if self._model_progress is not None
                else self.session.submit_task(values)
            )
            return _SessionHandle(
                job,
                self.output_root,
                self.audio_writer,
                self.worker_alive,
                self.quiesce,
                self.result_audit,
            )

    def close_when_idle(self):
        """Unload only an idle runtime; never manufacture cancellation/stop proof."""
        with self._lifecycle_lock:
            if self._closed:
                return
            if not self.is_idle():
                raise NotReady("wangp_session_active_shutdown_refused")
            self.quiesce()
            self.session.close()
            self._closed = True
            if self.device_lock is not None:
                self.device_lock.close()


def create_session(
    runtime_root, config_path, output_dir, *, manifest=None, modal_l40s=False
):
    """Call only after verify_runtime on an isolated, authorized engine process."""
    root = Path(runtime_root).resolve(strict=True)
    profile = None
    if manifest is not None:
        from ..runtime_catalog import validate_manifest

        profile = validate_manifest(manifest)
    if modal_l40s:
        from ..modal_runtime import device_profile

        if profile is None:
            raise ValueError('modal_v7_profile_required')
        profile = device_profile(profile)
    config = _check_config(
        config_path, deployment_profile_id=profile['id'] if profile else None
    )
    output = Path(output_dir).resolve(strict=True)
    existing = sys.modules.get("shared")
    if existing is not None and not Path(existing.__file__).resolve().is_relative_to(
        root
    ):
        raise ValueError("wangp_runtime_import_collision")
    sys.path.insert(0, str(root))
    device_lock = None
    if profile:
        mmgp = importlib.import_module('mmgp')
        if not Path(mmgp.__file__).resolve().is_relative_to(root):
            raise ValueError('wangp_runtime_import_collision')
        torch = importlib.import_module('torch')
        device_lock = _lock_profile_device(torch, profile)
        _profile_memory_admission(torch, profile)
    module = importlib.import_module("shared.api")
    if not Path(module.__file__).resolve().is_relative_to(root):
        raise ValueError("wangp_runtime_import_collision")
    session = module.init(
        root=root,
        config_path=config_path,
        output_dir=output,
        cli_args=profile['runtime']['cli_args']
        if profile
        else ["--attention", "sdpa", "--profile", "4"],
        console_output=False,
        console_isatty=False,
        webui_state=None,
    )
    torch = importlib.import_module("torch")

    def loaded_probe():
        native = session._ensure_runtime().module
        if getattr(native, "wan_model", None) is None:
            return "not_loaded"
        if profile and (
            native.transformer_type
            != model_for(profile['id'], manifest.document['mode'])['model_type']
            or native.loaded_profile != profile['runtime']['memory_profile']
            or native.loaded_config != profile['runtime']['task_config']
        ):
            return "not_observed"
        return "loaded"

    if profile:
        from ..runtime_catalog import model_for
    model_progress = ModelLoadProgress(loaded_probe)
    if profile:

        def audit(loaded=False):
            _audit_profile_runtime(session, manifest, config, loaded=loaded)
            if profile.get('compiler_epoch') == 'full-controls-v2':
                # Upstream initialization can select bundled tools and change
                # its own environment. Check that exact selection before Ready
                # and before work, never the CPU host's unrelated ffmpeg.
                from .wangp_media_tools import verify_media_tools

                verify_media_tools(root, dict(os.environ), cwd=Path.cwd())

        audit()
        return PinnedWanGPSession(
            session,
            output,
            quiesce=torch.cuda.synchronize,
            manifest=manifest,
            before_submit=audit,
            result_audit=lambda: audit(loaded=True),
            device_lock=device_lock,
            model_progress=model_progress,
        )
    return PinnedWanGPSession(
        session, output, quiesce=torch.cuda.synchronize, model_progress=model_progress
    )
