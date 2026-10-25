"""Explicit Linux package preparation; never runs at import or service startup.

prepare resolves/builds a wheelhouse on an authorized build host, installs a clean
verification venv, and records real package hashes/versions. No model is loaded.
"""
import argparse
from email.parser import BytesParser
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import zipfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from studio_platform.runtime_hosts.wangp_environment import (
    FORMAT, PYTHON, REVISION, REQUIREMENTS_SHA, canonical, digest, sha_file,
    regular_file, system_packages, verify_source)

PRIVATE_FILES = (
    "comfy_workflow.py",
    "studio_platform/__init__.py", "studio_platform/storage.py", "studio_platform/storage_config.py",
    "studio_platform/media_process.py", "studio_platform/media.py", "studio_platform/h3_profile_support.py",
    "studio_platform/inference/__init__.py", "studio_platform/inference/protocol.py",
    "studio_platform/inference/native_outputs.py",
    "studio_platform/inference/wangp.py", "studio_platform/inference/wangp_contract.py",
    "studio_platform/inference/wangp_delivery.py",
    "studio_platform/inference/wangp_compiler.py", "studio_platform/inference/wangp_http.py",
    "studio_platform/inference/wangp_ref_compiler.py",
    "studio_platform/inference/wangp_factory.py", "studio_platform/runtime_hosts/__init__.py",
    "studio_platform/runtime_hosts/wangp.py", "studio_platform/runtime_hosts/wangp_receipts.py",
    "studio_platform/runtime_hosts/wangp_delivery.py",
    "studio_platform/runtime_hosts/wangp_session.py", "studio_platform/runtime_hosts/wangp_http.py",
    "studio_platform/runtime_hosts/wangp_launcher.py", "studio_platform/runtime_hosts/wangp_environment.py",
    "studio_platform/runtime_hosts/wangp_startup.py",
    "studio_platform/runtime_hosts/wangp_system_restore.py",
    "studio_platform/runtime_hosts/wangp_download.py",
    "studio_platform/runtime_catalog.py", "studio_platform/inference/wangp_profile_compiler.py",
    "studio_platform/inference/wangp_profile_dispatch.py", "studio_platform/inference/wangp_full_compiler.py",
    "studio_platform/runtime_hosts/wangp_media_tools.py",
    "studio_platform/runtime_hosts/wangp_profile_bootstrap.py",
    "deploy/wangp/profiles/h3-pruned-rank8-int8-quanto-int8-vae-int8-sdpa-p4-lowram-v1.json",
    "deploy/wangp/profiles/h3-unpruned33b-int8-qwenbf16-vaefp16-sdpa-p3-lowram-v1.json",
    "deploy/wangp/profiles/h3-unpruned33b-bf16-qwenbf16-vaefp16-sdpa-p3-splitqkv-v2.json",
    "deploy/wangp/profiles/h3-pruned-rank8-int8-pro6000-quanto-int8-vae-int8-sdpa-p4-lowram-v1.json",
    "deploy/wangp/profiles/h3-pruned-rank8-int8-quanto-int8-vae-int8-sdpa-p4-29gib-v2.json",
    "deploy/wangp/profiles/h3-pruned-rank8-int8-quanto-int8-vae-int8-sdpa-p4-29gib-total-v3.json",
    "deploy/wangp/profiles/h3-pruned-rank8-int8-quanto-int8-vae-int8-sdpa-p4-29gib-cgroups-v4.json",
    "deploy/wangp/profiles/h3-unpruned33b-int8-qwenbf16-vaefp16-sdpa-p3-144gib-single-fl-v2.json",
    "deploy/wangp/profiles/h3-pruned-rank8-int8-sdpa-p4-29gib-fl-r2-v5.json",
    "deploy/wangp/profiles/h3-unpruned33b-int8-qwenbf16-vaefp16-sdpa-p3-144gib-fl-r2-v3.json",
    "deploy/wangp/profiles/h3-pruned-rank8-int8-sdpa-p4-29gib-controls-v6.json",
    "deploy/wangp/profiles/h3-pruned-rank8-int8-sdpa-p4-29gib-taomate5-v7.json",
    "deploy/wangp/profiles/h3-pruned-rank8-int8-pro6000-sdpa-p4-controls-v2.json",
    "deploy/wangp/profiles/h3-unpruned33b-int8-sdpa-p3-controls-v2.json",
    "deploy/wangp/profiles/h3-unpruned33b-bf16-sdpa-p3-controls-v3.json",
    "deploy/wangp/profiles/h3-unpruned33b-int8-sdpa-p3-144gib-single-controls-v4.json",
    "deploy/wangp/profiles/h3-unpruned33b-bf16-sdpa-p3-256gib-single-controls-v4.json",
    "deploy/wangp/targon_prepare.py",
    "deploy/wangp/probe_gpu.py",
)


def run(args, **kwargs):
    # Child output can contain index configuration; expose only stable errors.
    result = subprocess.run(args, capture_output=True, **kwargs)
    if result.returncode:
        detail = (result.stderr or b"")[-6000:]
        if isinstance(detail, bytes):
            detail = detail.decode("utf-8", errors="replace")
        # Explicit build-only diagnostics. Never retain auth/signed index URLs.
        print(re.sub(r"https?://\S+", "[redacted-url]", detail), file=sys.stderr)
        raise ValueError("wangp_package_command_failed")
    return result.stdout


def archive(path, folder):
    with tarfile.open(path, "w:gz") as target:
        for source in sorted(folder.rglob("*")):
            if source.is_symlink():
                raise ValueError("wangp_package_link_forbidden")
            if source.is_file():
                target.add(source, arcname=source.relative_to(folder).as_posix(), recursive=False)


def small_bundle(output, os_restore_kit=None, environment_lock=None):
    output = Path(output)
    extras = []
    if os_restore_kit:
        from studio_platform.runtime_hosts.wangp_system_restore import validate_kit, DIRECTORY
        if not environment_lock:
            raise ValueError("system_restore_environment_lock_required")
        lock = json.loads(Path(environment_lock).read_text(encoding="utf-8"))
        _, files = validate_kit(os_restore_kit, lock)
        extras = [(regular_file(Path(os_restore_kit), "manifest.json"), f"{DIRECTORY}/manifest.json")]
        extras.extend((path, f"{DIRECTORY}/{path.name}") for path in files)
    with tarfile.open(output, "x:gz") as target:
        for name in PRIVATE_FILES:
            target.add(regular_file(ROOT, name), arcname=name, recursive=False)
        for path, name in extras:
            target.add(path, arcname=name, recursive=False)
    if output.stat().st_size > 16 * 1024 * 1024:
        raise ValueError("wangp_source_bundle_limit")
    return {"filename": output.name, "sha256": sha_file(output), "size_bytes": output.stat().st_size}


def wheel_metadata(wheel):
    with zipfile.ZipFile(wheel) as package:
        # Some wheels vendor other packages' metadata. Only their own root
        # .dist-info/METADATA describes the installed distribution.
        candidates = [name for name in package.namelist()
                      if len(name.split("/")) == 2 and name.endswith(".dist-info/METADATA")]
        if len(candidates) != 1:
            raise ValueError("wangp_wheel_metadata_invalid")
        return BytesParser().parsebytes(package.read(candidates[0]))


def prepare(upstream, output, base_image, wheelhouse=None, system_debs="/var/cache/apt/archives"):
    if (platform.system() != "Linux" or platform.machine() != "x86_64"
            or not re.fullmatch(r"3\.11\.[0-9]+", platform.python_version())):
        raise ValueError("wangp_build_requires_linux_x86_64_python311")
    if not re.fullmatch(r"[^\s@]+@sha256:[0-9a-f]{64}", base_image):
        raise ValueError("wangp_base_image_digest_required")
    upstream = Path(upstream).resolve(strict=True)
    revision = run(["git", "-C", str(upstream), "rev-parse", "HEAD"]).decode().strip()
    if revision != REVISION:
        raise ValueError("wangp_upstream_revision_mismatch")
    run(["git", "-C", str(upstream), "diff", "--quiet", "HEAD", "--"])
    if sha_file(upstream / "requirements.txt") != REQUIREMENTS_SHA:
        raise ValueError("wangp_requirements_source_mismatch")
    output = Path(output).absolute()
    output.mkdir(exist_ok=False)  # Never overwrite a previous build/receipt.
    payload = output / "payload"
    runtime, wheels = payload / "upstream", payload / "wheels"
    runtime.mkdir(parents=True)
    wheels.mkdir()
    names = run(["git", "-C", str(upstream), "ls-files", "-z"]).decode().split("\0")
    sources = {}
    for name in filter(None, names):
        source = regular_file(upstream, name)
        target = runtime / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        sources[name] = {"sha256": sha_file(target), "size_bytes": target.stat().st_size}
    # Resolve the upstream full import surface and private host together. Building
    # upstream SageAttention is deliberately excluded: this recipe uses SDPA.
    env = dict(os.environ)
    for key in list(env):
        if key.startswith("PIP_"):
            del env[key]
    env["PIP_CONFIG_FILE"] = os.devnull
    if wheelhouse:
        print(json.dumps({"phase": "copying_existing_wheels"}), flush=True)
        for item in Path(wheelhouse).iterdir():
            source = regular_file(Path(wheelhouse), item.name)
            if source.suffix != ".whl":
                raise ValueError("wangp_nonwheel_dependency")
            shutil.copyfile(source, wheels / item.name)
    else:
        print(json.dumps({"phase": "resolving_dependency_wheels"}), flush=True)
        run([sys.executable, "-m", "pip", "wheel", "--wheel-dir", str(wheels),
             "--extra-index-url", "https://download.pytorch.org/whl/cu128",
             "-r", str(runtime / "requirements.txt"), "-r", str(Path(__file__).with_name("runtime-host.in"))], env=env)
    records, seen = [], set()
    for wheel in sorted(wheels.iterdir()):
        if wheel.suffix != ".whl":
            raise ValueError("wangp_nonwheel_dependency")
        metadata = wheel_metadata(wheel)
        name = re.sub(r"[-_.]+", "-", metadata["Name"]).lower()
        if name in seen or not re.fullmatch(r"[a-z0-9][a-z0-9-]*", name):
            raise ValueError("wangp_duplicate_wheel")
        seen.add(name)
        records.append({"file": wheel.name, "name": name, "version": metadata["Version"],
                        "sha256": sha_file(wheel), "size_bytes": wheel.stat().st_size})
    requirements = "".join(f"{r['name']}=={r['version']} --hash=sha256:{r['sha256']}\n"
                           for r in sorted(records, key=lambda r: r["name"]))
    (payload / "requirements.lock").write_text(requirements, encoding="utf-8")
    print(json.dumps({"phase": "verifying_offline_install", "wheel_count": len(records)}), flush=True)
    venv = output / "verification-venv"
    run([sys.executable, "-m", "venv", str(venv)])
    python = str(venv / "bin/python")
    run([python, "-m", "pip", "install", "--no-index", "--require-hashes", "--find-links", str(wheels),
         "-r", str(payload / "requirements.lock")], env=env)
    run([python, "-m", "pip", "check"], env=env)
    query = ("import importlib.metadata,json,re; print(json.dumps({re.sub(r'[-_.]+','-',d.metadata['Name']).lower():"
             "d.version for d in importlib.metadata.distributions()},sort_keys=True))")
    installed = json.loads(run([python, "-c", query]))
    debs = []
    deb_root = payload / "debs"
    deb_root.mkdir()
    for source in sorted(Path(system_debs).glob("*.deb")):
        source = regular_file(Path(system_debs), source.name)
        fields = run(["dpkg-deb", "-f", str(source), "Package", "Version", "Architecture"]).decode().splitlines()
        details = dict(line.split(": ", 1) for line in fields)
        target = deb_root / source.name
        shutil.copyfile(source, target)
        debs.append({"file": source.name, "name": details["Package"], "version": details["Version"],
                     "architecture": details["Architecture"], "sha256": sha_file(target), "size_bytes": target.stat().st_size})
    lock = {"format": FORMAT, "source_revision": REVISION, "python": platform.python_version(),
            "requirements_sha256": REQUIREMENTS_SHA, "base_image": base_image,
            "source_files": sources, "wheels": records, "installed_packages": installed,
            "system_packages": system_packages(), "debs": debs,
            "requirements_lock_sha256": sha_file(payload / "requirements.lock"),
            "qualification": "resolved-build-inputs-only"}
    (runtime / ".sixnine-environment.json").write_bytes(canonical(lock))
    verify_source(runtime, lock)
    artifact = output / "wangp-dependencies.tar.gz"
    archive(artifact, payload)
    result = {"environment_lock_sha256": digest(lock), "artifact_sha256": sha_file(artifact),
              "artifact_size_bytes": artifact.stat().st_size, "base_image": base_image,
              "inference_verified": False, "image_qualified": False}
    (output / "build-receipt.json").write_bytes(canonical(result))
    return result


def bind_manifest(source, environment_lock, output, image):
    from studio_platform.runtime_hosts.wangp_environment import validate_lock
    from studio_platform.runtime_hosts.wangp_session import CORE_VERSIONS
    if not re.fullmatch(r"[^\s@]+@sha256:[0-9a-f]{64}", image):
        raise ValueError("wangp_runtime_image_digest_required")
    value = json.loads(Path(source).read_text(encoding="utf-8"))
    lock = validate_lock(json.loads(Path(environment_lock).read_text(encoding="utf-8")))
    if any(lock["installed_packages"].get(name) != version for name, version in CORE_VERSIONS.items()):
        raise ValueError("wangp_manifest_core_versions_mismatch")
    value.update(runtime_digest=digest(lock), runtime_digest_kind="sixnine-environment-lock-sha256",
                 runtime_image=image, inference_qualified=False)
    value["runtime_recipe"].update(python=lock["python"], full_dependency_lock=digest(lock), image_digest=image,
                                 cuda_recipe="PyTorch cu128 wheels; exact native packages in the environment lock",
                                 image_qualification="build identity only; GPU execution unverified")
    # A new identity; never overwrite the historical candidate/accepted manifest.
    with Path(output).open("xb") as target:
        target.write(canonical(value))
    return {"manifest_digest": digest(value), "runtime_image": image, "inference_verified": False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    item = sub.add_parser("prepare")
    for name in ("upstream-root", "output", "base-image"):
        item.add_argument("--" + name, required=True)
    item.add_argument("--wheelhouse", help="Reuse an existing wheelhouse offline instead of resolving dependencies")
    item.add_argument("--system-debs", default="/var/cache/apt/archives", help="Retained installed system package archives to include for offline bootstrap")
    item = sub.add_parser("source-bundle")
    item.add_argument("--output", required=True)
    item.add_argument("--os-restore-kit", help="Optional finite, locally verified SSH drift repair kit; never installed on the build host")
    item.add_argument("--environment-lock", help="Original unchanged environment lock required when including an OS restore kit")
    item = sub.add_parser("bind-manifest")
    for name in ("manifest", "environment-lock", "output", "image"):
        item.add_argument("--" + name, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "prepare":
            result = prepare(args.upstream_root, args.output, args.base_image, args.wheelhouse, args.system_debs)
        elif args.command == "source-bundle":
            result = small_bundle(args.output, args.os_restore_kit, args.environment_lock)
        else:
            result = bind_manifest(args.manifest, args.environment_lock, args.output, args.image)
        print(json.dumps(result))
        return 0
    except Exception:
        print(json.dumps({"state": "package_preparation_failed"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
