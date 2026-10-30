"""Bounded public model fetching; inert until explicitly invoked by bootstrap.

The already locked Hugging Face SDK owns transfer retries/partial-file formats.
This helper cannot start a model, change a manifest, rent a GPU or replay a job.
Downloaded bytes remain unqualified until the existing full runtime verifier.
"""

from __future__ import annotations

import argparse
import ctypes
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
import json
import logging
import math
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time

from ..inference.wangp_contract import EngineManifest

ENDPOINT = "https://huggingface.co"
MAX_FILES = 64
MAX_TOTAL = 256 * 1024**3
CACHE_LIMIT = 1024**3
HEADROOM = 10 * 1024**3
DOWNLOAD_TIMEOUT = 7200
# Explicit transport identity, reviewed against all eleven original FL/REF files.
# This is selected before transfer; HTTP failures never select another revision.
# Official immutable metadata response SHA256:
# df25843addd3d71ebbc2041e0900b3a7185a4a9fb93a4a1c4c5f488d7d73041b
_LOCATOR_PROFILE = "h3-pruned-rank8-int8-quanto-int8-vae-int8-sdpa-p4-29gib-total-v3"
_LOCATOR_REPOSITORY = "DeepBeepMeep/MiniMax-H3"
_LOCATOR_ORIGINAL = "adc81ccb71352192214d83d5fafb9487e860be39"
_LOCATOR_REVISION = "8d9093438ffb3e33d6a058cc20fa3b7ca31f5752"
_LOCATOR_MANIFESTS = {
    "fl": "e32a141d43774d2e1b61bb45432aea2a2bde3e66e36318c84dd8d4538e64d530",
    "ref": "47e46d47404c198c667bcb5114b4680bab5ed0aac60c5f77c610a197d62d7367",
}
_LOCATOR_PROFILES = {
    _LOCATOR_PROFILE: _LOCATOR_MANIFESTS,
    "h3-pruned-rank8-int8-quanto-int8-vae-int8-sdpa-p4-29gib-cgroups-v4": {
        "fl": "a92ae3dddb83ab73e4ac292aa7b33f9ea158ac638523d0b0c1e2e771cb0e7517",
        "ref": "a096e69c6b91d6b7200d414e72db870c0198ea02ebcc7ea16bf787091f66d84c",
    },
    "h3-unpruned33b-int8-qwenbf16-vaefp16-sdpa-p3-144gib-single-fl-v2": {
        "fl": "a50aff06326dcb51dcd74bef968fb452a2d2804e07489d032a460bac4a11dc2d",
    },
    "h3-pruned-rank8-int8-sdpa-p4-29gib-fl-r2-v5": {
        "fl": "06f5b7cb298bbbd578cee2860e9348f9458693397d6ca95aa0efcef9947e24a6",
    },
    "h3-unpruned33b-int8-qwenbf16-vaefp16-sdpa-p3-144gib-fl-r2-v3": {
        "fl": "0e5e6ad95dc6c28177c2c008c43fb2f26b337a5ce44b539779703a024dd2d704",
    },
}
_LOCATOR_TRANSFORMERS = {
    "fl": "MiniMax-H3-FL2VA-pruned_rank8_int8_convrot.safetensors",
    "ref": "MiniMax-H3-Ref2VA-pruned_rank8_int8_convrot.safetensors",
}
# path, original byte size, original hash kind and original content hash.
_LOCATOR_FILES = (
    (
        _LOCATOR_TRANSFORMERS["fl"],
        21057674787,
        "sha256",
        "30ff400f974b11a1ef13d216c5d9f6439a9c10322a3988b0374a39672ce286f0",
    ),
    (
        _LOCATOR_TRANSFORMERS["ref"],
        21057674788,
        "sha256",
        "e09db861c48d13560222948b4e38082bc21b1b7d4ddcaed9c865bf9c3233898c",
    ),
    (
        "Qwen3-VL-32B-Instruct/Qwen3-VL-32B-Instruct-layer50_quanto_bf16_int8.safetensors",
        26723791903,
        "sha256",
        "4df8fc5237746b3b058745d6ec8fe1e54a9721bdc663d35d2d1806952672f301",
    ),
    (
        "Qwen3-VL-32B-Instruct/config.json",
        1474,
        "git_blob_sha1",
        "29e925092dee9f14278c53e7e7d876cea8a997bc",
    ),
    (
        "Qwen3-VL-32B-Instruct/preprocessor_config.json",
        390,
        "git_blob_sha1",
        "2ea84a437d448ff71b08df68fdd949d5cc4ebb64",
    ),
    (
        "Qwen3-VL-32B-Instruct/tokenizer.json",
        7032403,
        "git_blob_sha1",
        "c6cc1014128b19d1fc46b1d30a23e3b1d35db421",
    ),
    (
        "Qwen3-VL-32B-Instruct/tokenizer_config.json",
        11004,
        "git_blob_sha1",
        "5e463a18949779b8a38a03f0a6d9089094970eed",
    ),
    (
        "Qwen3-VL-32B-Instruct/vocab.json",
        2776833,
        "git_blob_sha1",
        "4783fe10ac3adce15ac8f358ef5462739852c569",
    ),
    (
        "minimax_h3/minimax_h3_video_vae_int8_convrot.safetensors",
        2811065184,
        "sha256",
        "52a2c8c73583c86e4f41cdcce3a6ad0ea562987bc0bf3d60a0cef5f5c8e60c0e",
    ),
    (
        "MiniMax-H3-audio_vae_fp32.safetensors",
        605429308,
        "sha256",
        "37dddc2f3e6d5d5139d823d5ea283bbf304dadcb885b1ccda818aa13dade5ea2",
    ),
    (
        "minimax_h3/minimax_h3_latent_upscaler_3d_bf16.safetensors",
        690592992,
        "sha256",
        "4f57821f5837f32f7142b67d815606dbd7550f194e5c769f7d6c3f83b146a5e6",
    ),
)
# New Base FL binding only; old Base manifests keep their original transport.
# All ten files' size and hash match official immutable metadata at
# _LOCATOR_REVISION. Metadata response SHA256 (not payload/inference proof):
# a6c7e67b7e19a1ebcbd7a7bd84ae8c4eb92cd2cd14a5dacd872cc8eb56ee4316
_BASE_FL_LOCATOR_FILES = (
    (
        "MiniMax-H3-FL2VA_int8_convrot.safetensors",
        34038903007,
        "sha256",
        "83a36b67776962f44087f2f7c12d95791393f3cce1ef898efc90405216e7b0c0",
    ),
    (
        "Qwen3-VL-32B-Instruct/Qwen3-VL-32B-Instruct-layer50_bf16.safetensors",
        51506305568,
        "sha256",
        "a91ca0cc41bead2e62d8fc044a3ffdbd9487daa7c8997f697049990c7178e0a6",
    ),
    (
        "MiniMax-H3-video_vae_fp16.safetensors",
        5207806512,
        "sha256",
        "455010492bb59a9cc7b8f1ee23905b22a10079f89490adbf820a1728efcaea6b",
    ),
    *tuple(
        row
        for row in _LOCATOR_FILES
        if row[2] == "git_blob_sha1"
        or row[0]
        in {
            "MiniMax-H3-audio_vae_fp32.safetensors",
            "minimax_h3/minimax_h3_latent_upscaler_3d_bf16.safetensors",
        }
    ),
)
# All seventeen files used by the published full-controls catalog match the
# official immutable _LOCATOR_REVISION metadata (sizes + LFS SHA256/Git blob ID).
# Metadata-only response SHA256, not downloaded-weight or inference evidence:
# 3ab00f8274e8f72538845415e28523556006f218eee430e35f9b54723ff9aebc
_BASE_COMMON_LOCATOR_FILES = tuple(
    row
    for row in _BASE_FL_LOCATOR_FILES
    if row[0] != "MiniMax-H3-FL2VA_int8_convrot.safetensors"
)
_BASE_REF_LOCATOR_FILES = (
    (
        "MiniMax-H3-Ref2VA_int8_convrot.safetensors",
        34038903008,
        "sha256",
        "d9ba725b2920bb5ffb4ab8e2514f77264df59a5f05513613513f6fb5d9d6f749",
    ),
    *_BASE_COMMON_LOCATOR_FILES,
)
_BF16_FL_LOCATOR_FILES = (
    (
        "MiniMax-H3-FL2VA_bf16.safetensors",
        66280486944,
        "sha256",
        "f10704606e376815298cb6bd9d94ea721346b62cd840177cd3723971edd83367",
    ),
    *_BASE_COMMON_LOCATOR_FILES,
)
_BF16_REF_LOCATOR_FILES = (
    (
        "MiniMax-H3-Ref2VA_bf16.safetensors",
        66280486944,
        "sha256",
        "ca877ed2b1bf72cfda76fe38117832544d6c0530abcfb9463e114cd767a39516",
    ),
    *_BASE_COMMON_LOCATOR_FILES,
)
# Owner default 2026-10-25: WanGP's rank-19 TaoMate turbo LoRA. Size and LFS
# SHA256 match official immutable _LOCATOR_REVISION metadata for this path.
_TAOMATE_LORA = (
    "loras/minimax_h3_taomate_3step_lora_avg_rank_19_bf16.safetensors",
    181697688,
    "sha256",
    "de9663d974a884b477556748239c6f28239f7ca1825be270f98f023ff5dab6a7",
)
_VAE_ORIGINAL = next(
    row
    for row in _LOCATOR_FILES
    if row[0] == "minimax_h3/minimax_h3_video_vae_int8_convrot.safetensors"
)
# Newer immutable files fetched from their own revision (per-file locator). Each
# row is approved only at exactly this path, size, hash and revision (#844).
_DIRECT_FILES = {
    # v9 (WanGP 9cb2bcd): the same VAE with ConvRot file-level format metadata,
    # the file 9cb2bcd was measured with (2026-10-11, +72 bytes).
    (
        "minimax_h3/minimax_h3_video_vae_int8_convrot.safetensors",
        2811065256,
        "sha256",
        "0cb8161a12166fb049a2c1295e6a4833fdc571effdab9ac0ed074b10f2d6646d",
    ): "98b5288b174fe08f6d54f9af0dc46ee8bb58b225",
    # Pika H3 Flex adapters for the v9 Pika profile (#844 stage 2), one per mode.
    (
        "loras/pika_h3_flex_fl2va_6step_v1_bf16.safetensors",
        1383678576,
        "sha256",
        "d2e7b5ded8671fd480dc92b86beca078708e90f4712e73670111ea8adb98988f",
    ): "2375fd1c3475ea21cc1bfa9761c5ab79f0cde3ea",
    (
        "loras/pika_h3_flex_ref2va_6step_v1_bf16.safetensors",
        1383678576,
        "sha256",
        "202e31cd53963fc9ad7692542b911024c6d2025c37e8f2c143bca5179cb10e98",
    ): "2375fd1c3475ea21cc1bfa9761c5ab79f0cde3ea",
}
_V9_VAE = next(row for row in _DIRECT_FILES if row[0] == _VAE_ORIGINAL[0])
_PIKA_LORAS = {
    mode: next(row for row in _DIRECT_FILES if "_" + name + "_6step" in row[0])
    for mode, name in (("fl", "fl2va"), ("ref", "ref2va"))
}


def _with_v9_vae(rows):
    return tuple(_V9_VAE if row == _VAE_ORIGINAL else row for row in rows)


_FULL_CONTROL_LOCATOR_FILES = {
    "fl": (
        tuple(row for row in _LOCATOR_FILES if row[0] != _LOCATOR_TRANSFORMERS["ref"]),
        _BASE_FL_LOCATOR_FILES,
        _BF16_FL_LOCATOR_FILES,
        (
            *(row for row in _LOCATOR_FILES if row[0] != _LOCATOR_TRANSFORMERS["ref"]),
            _TAOMATE_LORA,
        ),
        _with_v9_vae(
            (
                *(
                    row
                    for row in _LOCATOR_FILES
                    if row[0] != _LOCATOR_TRANSFORMERS["ref"]
                ),
                _TAOMATE_LORA,
            )
        ),
        _with_v9_vae(
            (
                *(
                    row
                    for row in _LOCATOR_FILES
                    if row[0] != _LOCATOR_TRANSFORMERS["ref"]
                ),
                _PIKA_LORAS["fl"],
            )
        ),
    ),
    "ref": (
        tuple(row for row in _LOCATOR_FILES if row[0] != _LOCATOR_TRANSFORMERS["fl"]),
        _BASE_REF_LOCATOR_FILES,
        _BF16_REF_LOCATOR_FILES,
        (
            *(row for row in _LOCATOR_FILES if row[0] != _LOCATOR_TRANSFORMERS["fl"]),
            _TAOMATE_LORA,
        ),
        _with_v9_vae(
            (
                *(
                    row
                    for row in _LOCATOR_FILES
                    if row[0] != _LOCATOR_TRANSFORMERS["fl"]
                ),
                _TAOMATE_LORA,
            )
        ),
        _with_v9_vae(
            (
                *(
                    row
                    for row in _LOCATOR_FILES
                    if row[0] != _LOCATOR_TRANSFORMERS["fl"]
                ),
                _PIKA_LORAS["ref"],
            )
        ),
    ),
}
SAFE_ERRORS = frozenset(
    {
        "model_download_manifest_invalid",
        "model_download_manifest_changed",
        "model_download_locator_invalid",
        "model_download_path_invalid",
        "model_download_size_mismatch",
        "model_download_disk_headroom",
        "model_download_failed",
        "model_download_timeout",
        "model_download_cache_limit",
        "model_download_progress_invalid",
        "model_download_stop_unconfirmed",
        "model_download_state_exists",
        "model_download_owner_unconfirmed",
    }
)
STOP_STATUSES = frozenset({"confirmed", "unconfirmed", "not_observed"})


def safe_download_failure(value):
    """Diagnostic facts only; these never substitute for controller idle proof."""
    if (
        not isinstance(value, dict)
        or not isinstance(value.get("error_code"), str)
        or value["error_code"] not in SAFE_ERRORS
        or not isinstance(value.get("stop_status"), str)
        or value["stop_status"] not in STOP_STATUSES
    ):
        return None
    return {"error_code": value["error_code"], "stop_status": value["stop_status"]}


class DownloadFailure(ValueError):
    def __init__(self, error_code, stop_status):
        self.diagnosis = safe_download_failure(
            {"error_code": error_code, "stop_status": stop_status}
        )
        if self.diagnosis is None:
            raise ValueError("model_download_progress_invalid")
        # Stop uncertainty remains the outward failure. Preserve its initiating
        # cause separately, never downgrade this to an ordinary stopped failure.
        super().__init__(
            "model_download_stop_unconfirmed"
            if stop_status == "unconfirmed"
            else error_code
        )


def path_checked(value):
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("model_download_path_invalid")
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            info = current.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or not (
            stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)
        ):
            raise ValueError("model_download_path_invalid")
        if stat.S_ISREG(info.st_mode) and info.st_nlink != 1:
            raise ValueError("model_download_path_invalid")
    return path


def selected_files(document, expected_digest):
    """Build an immutable exact-file list, never snapshot an entire repository."""
    try:
        manifest = EngineManifest.from_dict(document)
        if manifest.digest != expected_digest:
            raise ValueError("model_download_manifest_changed")
        records, names = [], set()
        for name, component in manifest.document["components"].items():
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", name):
                raise ValueError()
            repo, revision = component.get("repository"), component["revision"]
            if (
                not isinstance(repo, str)
                or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo)
                or any(part in {".", ".."} for part in repo.split("/"))
                or not re.fullmatch(r"[0-9a-f]{40}", revision)
            ):
                raise ValueError()
            files = component.get("files")
            if not isinstance(files, list) or not files:
                raise ValueError()
            for item in files:
                filename = item["path"]
                relative = PurePosixPath(filename)
                if (
                    not isinstance(filename, str)
                    or not filename
                    or relative.is_absolute()
                    or str(relative) != filename
                    or any(x in {".", "..", ".cache"} for x in relative.parts)
                    or re.search(r"[\\:*?\[\]#%\x00-\x20]", filename)
                    or filename in names
                    or type(item["size_bytes"]) is not int
                    or not 0 < item["size_bytes"] <= MAX_TOTAL
                ):
                    raise ValueError()
                key, width = (
                    ("sha256", 64) if "sha256" in item else ("git_blob_sha1", 40)
                )
                if not re.fullmatch(
                    r"[0-9a-f]{" + str(width) + r"}", item.get(key, "")
                ):
                    raise ValueError()
                records.append((name, repo, revision, filename, item["size_bytes"]))
                names.add(filename)
        if (
            not 0 < len(records) <= MAX_FILES
            or sum(row[4] for row in records) > MAX_TOTAL
        ):
            raise ValueError()
        return tuple(records)
    except Exception as error:
        code = str(error)
        raise ValueError(
            code
            if code == "model_download_manifest_changed"
            else "model_download_manifest_invalid"
        ) from None


def safe_error(error):
    code = str(error)
    return code if code in SAFE_ERRORS else "model_download_failed"


def _content_preserving_revision(document, expected_digest, records):
    """Use one approved locator without changing the immutable model contract."""
    manifests = _LOCATOR_PROFILES.get(document.get("deployment_profile_id"))
    mode = document.get("mode")
    full_controls = False
    if manifests is None:
        from ..runtime_catalog import engine_manifest, get_profile

        try:
            native = get_profile(document.get("deployment_profile_id"))
        except ValueError:
            return None
        if native.get("compiler_epoch") != "full-controls-v2":
            return None
        try:
            manifests = {mode: engine_manifest(native["id"], mode).digest}
        except ValueError:
            raise ValueError("model_download_locator_invalid") from None
        full_controls = True
    if manifests.get(mode) != expected_digest or any(
        repo != _LOCATOR_REPOSITORY
        or revision not in {_LOCATOR_ORIGINAL, *_DIRECT_FILES.values()}
        for _, repo, revision, _, _ in records
    ):
        raise ValueError("model_download_locator_invalid")
    files, direct = [], {}
    for component in document["components"].values():
        for item in component["files"]:
            kind = "sha256" if "sha256" in item else "git_blob_sha1"
            row = (item["path"], item["size_bytes"], kind, item[kind])
            files.append(row)
            # A direct row and only a direct row names its own newer revision.
            if (component["revision"] != _LOCATOR_ORIGINAL) != (row in _DIRECT_FILES):
                raise ValueError("model_download_locator_invalid")
            if row in _DIRECT_FILES:
                if component["revision"] != _DIRECT_FILES[row]:
                    raise ValueError("model_download_locator_invalid")
                direct[row[0]] = _DIRECT_FILES[row]
    if full_controls:
        expected_sets = _FULL_CONTROL_LOCATOR_FILES[mode]
    elif document.get("deployment_profile_id") in {
        "h3-unpruned33b-int8-qwenbf16-vaefp16-sdpa-p3-144gib-single-fl-v2",
        "h3-unpruned33b-int8-qwenbf16-vaefp16-sdpa-p3-144gib-fl-r2-v3",
    }:
        expected_sets = (_BASE_FL_LOCATOR_FILES,)
    else:
        omitted = _LOCATOR_TRANSFORMERS["ref" if mode == "fl" else "fl"]
        expected_sets = (tuple(row for row in _LOCATOR_FILES if row[0] != omitted),)
    if not any(
        sorted(files) == sorted(expected_files) for expected_files in expected_sets
    ):
        raise ValueError("model_download_locator_invalid")
    if direct:
        return {
            filename: direct.get(filename, _LOCATOR_REVISION)
            for _, _, _, filename, _ in records
        }
    return _LOCATOR_REVISION


def write_progress(path, value):
    path = path_checked(path)
    temporary = path_checked(path.with_suffix(".tmp"))
    raw = json.dumps(value, sort_keys=True, allow_nan=False).encode()
    if len(raw) > 8192:
        raise ValueError("model_download_progress_invalid")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as stream:
        os.fchmod(stream.fileno(), 0o600) if hasattr(os, "fchmod") else None
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def record_failure(state, expected_digest, error):
    """The owner has a separate file; a still-running child cannot overwrite it."""
    write_progress(
        state / "failure.json",
        {
            "state": "failed",
            "manifest_digest": expected_digest,
            "error_code": str(error),
            "download_failure": error.diagnosis,
        },
    )
    if os.name == "posix":
        fd = os.open(state, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def download_environment(environment, state_root):
    # No implicit API identity, custom endpoint or inherited SDK debug settings.
    state = path_checked(state_root)
    result = {
        key: value
        for key, value in environment.items()
        if not key.startswith(("HF_", "HUGGINGFACE_", "HUGGING_FACE_"))
    }
    result.update(
        HF_ENDPOINT=ENDPOINT,
        HF_HOME=str(state / "cache"),
        HF_HUB_CACHE=str(state / "cache" / "hub"),
        HF_XET_CACHE=str(state / "cache" / "xet"),
        HF_TOKEN_PATH=str(state / "no-token"),
        HF_HUB_DISABLE_IMPLICIT_TOKEN="1",
        HF_HUB_OFFLINE="0",
        HF_HUB_DISABLE_TELEMETRY="1",
        HF_HUB_DISABLE_PROGRESS_BARS="1",
        HF_HUB_VERBOSITY="error",
        HF_DEBUG="0",
        HF_HUB_DOWNLOAD_TIMEOUT="60",
        HF_HUB_ETAG_TIMEOUT="30",
        HF_XET_HIGH_PERFORMANCE="0",
        HF_XET_CHUNK_CACHE_SIZE_BYTES="0",
        HF_XET_SHARD_CACHE_SIZE_LIMIT="0",
        HF_XET_LOG_DEST=os.devnull,
        HF_XET_LOG_FILE=os.devnull,
        RUST_LOG="off",
        RUST_BACKTRACE="0",
        DO_NOT_TRACK="1",
        TRANSFORMERS_OFFLINE="1",
    )
    return result


def cache_size(root):
    root = path_checked(root)
    if not root.exists():
        return 0
    size = count = 0
    for parent, dirs, files in os.walk(root, followlinks=False):
        for name in dirs + files:
            path = path_checked(Path(parent) / name)
            count += 1
            if count > 4096:
                raise ValueError("model_download_cache_limit")
            if path.is_file():
                size += path.stat().st_size
            if size > CACHE_LIMIT:
                raise ValueError("model_download_cache_limit")
    return size


def fetch_files(
    document,
    expected_digest,
    model_root,
    progress_path,
    *,
    workers=2,
    downloader=None,
    sleep=time.sleep,
):
    records = selected_files(document, expected_digest)
    locator_revision = _content_preserving_revision(document, expected_digest, records)
    if type(workers) is not int or not 1 <= workers <= 2:
        raise ValueError("model_download_manifest_invalid")
    root = path_checked(model_root)
    root.mkdir(parents=True, exist_ok=True)
    # Full incomplete objects may be preallocated by Xet. Do not report stat size
    # as received network bytes, or subtract it from this conservative disk gate.
    remaining = 0
    for record in records:
        target = path_checked(root / record[3])
        if target.exists():
            if not target.is_file() or target.stat().st_size != record[4]:
                raise ValueError("model_download_size_mismatch")
        else:
            remaining += record[4]
    if shutil.disk_usage(root).free < remaining + HEADROOM:
        raise ValueError("model_download_disk_headroom")
    if downloader is None:
        from huggingface_hub import hf_hub_download

        downloader = hf_hub_download
    lock = threading.Lock()
    completed = set()
    failure = None
    total = sum(row[4] for row in records)

    def progress(state, code=None):
        value = {
            "state": state,
            "manifest_digest": expected_digest,
            "files_complete": len(completed),
            "files_total": len(records),
            "bytes_complete": sum(row[4] for row in records if row[3] in completed),
            "bytes_total": total,
            "inference_verified": False,
        }
        if code:
            value["code"] = code
        write_progress(progress_path, value)

    progress("downloading")

    def fetch(record):
        nonlocal failure
        _, repo, revision, filename, size = record
        expected = path_checked(root / filename)
        for attempt in range(2):
            with lock:
                if failure is not None:
                    raise ValueError(failure)
            try:
                returned = downloader(
                    repo_id=repo,
                    filename=filename,
                    revision=locator_revision.get(filename)
                    if isinstance(locator_revision, dict)
                    else locator_revision or revision,
                    local_dir=str(root),
                    token=False,
                    endpoint=ENDPOINT,
                    force_download=False,
                    etag_timeout=30,
                )
                path = path_checked(returned)
                if path != expected or not path.is_file():
                    raise ValueError("model_download_path_invalid")
                if path.stat().st_size != size:
                    raise ValueError("model_download_size_mismatch")
                break
            except Exception as error:
                # SDK errors may contain transient signed URLs. Never retain or
                # serialize them. Only a file transfer retries, not bootstrap.
                if str(error) in SAFE_ERRORS or attempt == 1:
                    with lock:
                        if failure is None:
                            failure = safe_error(error)
                            # Tell the parent now, before executor shutdown waits
                            # for another native transfer. Only it stops/reaps us.
                            progress("failed", failure)
                    raise ValueError(failure) from None
                sleep(1)
        with lock:
            if failure is None:
                completed.add(filename)
                progress("downloading")

    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(fetch, records))
    except Exception as error:
        with lock:
            progress("failed", safe_error(error))
        raise ValueError(safe_error(error)) from None
    progress("downloaded_unverified")


def stop_child(child):
    if child.poll() is None:
        try:
            if os.name == "posix":
                os.killpg(child.pid, signal.SIGKILL)
            else:
                child.kill()
        except ProcessLookupError:
            pass
    try:
        child.wait(timeout=10)
    except Exception:
        raise ValueError("model_download_stop_unconfirmed") from None


def child_guard(owner_pid, timeout):
    """Linux kernel guards survive parent termination and native SDK stalls."""
    if (
        sys.platform != "linux"
        or type(owner_pid) is not int
        or owner_pid <= 1
        or type(timeout) not in (int, float)
        or not math.isfinite(timeout)
        or not 0 < timeout <= DOWNLOAD_TIMEOUT
        or os.getppid() != owner_pid
    ):
        raise ValueError("model_download_owner_unconfirmed")
    libc = ctypes.CDLL(None, use_errno=True)
    # The child is a separate process group so the owner can stop/reap it. Bind
    # death to that original owner too; a session boundary must not orphan it.
    if libc.prctl(1, signal.SIGKILL, 0, 0, 0) != 0 or os.getppid() != owner_pid:
        raise ValueError("model_download_owner_unconfirmed")
    signal.signal(signal.SIGALRM, signal.SIG_DFL)
    signal.setitimer(signal.ITIMER_REAL, timeout)


def run_download(
    python,
    source,
    manifest_path,
    expected_digest,
    model_root,
    state_root,
    environment,
    progress,
    *,
    timeout=DOWNLOAD_TIMEOUT,
    workers=2,
    popen=subprocess.Popen,
    clock=time.monotonic,
    sleep=time.sleep,
    stop=stop_child,
):
    if (
        type(timeout) not in (int, float)
        or not math.isfinite(timeout)
        or not 1 <= timeout <= DOWNLOAD_TIMEOUT
        or type(workers) is not int
        or not 1 <= workers <= 2
    ):
        raise ValueError("model_download_manifest_invalid")
    manifest_path, root, state = map(
        path_checked, (manifest_path, model_root, state_root)
    )
    if manifest_path.stat().st_size > 1024**2:
        raise ValueError("model_download_manifest_invalid")
    records = selected_files(json.loads(manifest_path.read_text()), expected_digest)
    root.mkdir(parents=True, exist_ok=True)
    try:
        state.mkdir(mode=0o700, parents=False, exist_ok=False)
    except FileExistsError:
        raise ValueError("model_download_state_exists") from None
    receipt = state / "progress.json"
    command = [
        str(python),
        "-m",
        "studio_platform.runtime_hosts.wangp_download",
        "--manifest",
        str(manifest_path),
        "--expected-digest",
        expected_digest,
        "--model-root",
        str(root),
        "--progress",
        str(receipt),
        "--workers",
        str(workers),
        "--owner-pid",
        str(os.getpid()),
        "--timeout",
        str(timeout),
    ]

    def read_receipt():
        value = None
        if receipt.exists():
            path_checked(receipt)
            if receipt.stat().st_size > 8192:
                raise ValueError("model_download_progress_invalid")
            value = json.loads(receipt.read_text())
            if (
                set(value)
                - {
                    "state",
                    "manifest_digest",
                    "files_complete",
                    "files_total",
                    "bytes_complete",
                    "bytes_total",
                    "inference_verified",
                    "code",
                }
                or value.get("state")
                not in {"downloading", "downloaded_unverified", "failed"}
                or value.get("manifest_digest") != expected_digest
                or value.get("inference_verified") is not False
                or value.get("files_total") != len(records)
                or value.get("bytes_total") != sum(r[4] for r in records)
                or type(value.get("files_complete")) is not int
                or not 0 <= value["files_complete"] <= len(records)
                or type(value.get("bytes_complete")) is not int
                or not 0 <= value["bytes_complete"] <= value["bytes_total"]
                or value.get("code", "model_download_failed") not in SAFE_ERRORS
            ):
                raise ValueError("model_download_progress_invalid")
            if value["state"] == "failed":
                raise ValueError(value.get("code", "model_download_failed"))
        return value

    deadline = clock() + timeout
    child = None
    last = None
    try:
        child = popen(
            command,
            cwd=str(path_checked(source)),
            env=download_environment(environment, state),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        while True:
            if clock() >= deadline:
                raise ValueError("model_download_timeout")
            cache_size(state / "cache")
            if shutil.disk_usage(root).free < HEADROOM:
                raise ValueError("model_download_disk_headroom")
            value = read_receipt()
            if value is not None and value != last:
                progress(
                    "model_download",
                    files_complete=value["files_complete"],
                    files_total=value["files_total"],
                    bytes_complete=value["bytes_complete"],
                    bytes_total=value["bytes_total"],
                )
                last = value
            result = child.poll()
            if result is not None:
                child.wait(timeout=10)
                # A final atomic receipt may arrive between the prior read and
                # poll. Validate it after the exact child has exited/reaped.
                value = read_receipt()
                if (
                    result != 0
                    or value is None
                    or value["state"] != "downloaded_unverified"
                    or value["files_complete"] != len(records)
                    or value["bytes_complete"] != value["bytes_total"]
                ):
                    raise ValueError(
                        value.get("code", "model_download_failed")
                        if value
                        else "model_download_failed"
                    )
                # The existing verify-only and owned-launch checks still hash
                # every model. A downloader receipt never certifies model bytes.
                return value
            sleep(0.25)
    except BaseException as original:
        error_code = safe_error(original)
        # No returned Popen handle is not proof that a process never existed.
        failure = DownloadFailure(
            error_code, "unconfirmed" if child is not None else "not_observed"
        )
        try:
            record_failure(state, expected_digest, failure)
        except Exception:
            # A full/broken disk must not prevent stopping the exact child or
            # replace either safe cause. Bootstrap can still retain the fields.
            pass
        if child is not None:
            try:
                stop(child)
            except BaseException:
                raise failure from None
            failure = DownloadFailure(error_code, "confirmed")
            try:
                record_failure(state, expected_digest, failure)
            except Exception:
                pass  # An earlier unconfirmed receipt remains conservative.
        if not isinstance(original, Exception):
            raise  # Preserve KeyboardInterrupt/SystemExit after cleanup.
        raise failure from None


def main(argv=None, *, guard=child_guard):
    parser = argparse.ArgumentParser(description="Pinned public model preparation only")
    for name in ("manifest", "model-root", "progress"):
        parser.add_argument("--" + name, required=True, type=Path)
    parser.add_argument("--expected-digest", required=True)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--owner-pid", type=int, required=True)
    parser.add_argument("--timeout", type=float, default=DOWNLOAD_TIMEOUT)
    args = parser.parse_args(argv)
    prior_logging = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        guard(args.owner_pid, args.timeout)
        manifest = path_checked(args.manifest)
        if manifest.stat().st_size > 1024**2:
            raise ValueError("model_download_manifest_invalid")
        # Production also redirects file descriptors, covering native SDK logs.
        with (
            open(os.devnull, "w") as quiet,
            redirect_stdout(quiet),
            redirect_stderr(quiet),
        ):
            fetch_files(
                json.loads(manifest.read_text()),
                args.expected_digest,
                args.model_root,
                args.progress,
                workers=args.workers,
            )
        return 0
    except Exception as error:
        # Preserve only static codes. No raw exception, URLs or credentials.
        try:
            records = selected_files(
                json.loads(args.manifest.read_text()), args.expected_digest
            )
            write_progress(
                args.progress,
                {
                    "state": "failed",
                    "manifest_digest": args.expected_digest,
                    "files_complete": 0,
                    "files_total": len(records),
                    "bytes_complete": 0,
                    "bytes_total": sum(r[4] for r in records),
                    "inference_verified": False,
                    "code": safe_error(error),
                },
            )
        except Exception:
            pass
        return 1
    finally:
        logging.disable(prior_logging)


if __name__ == "__main__":
    raise SystemExit(main())
