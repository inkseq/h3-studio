"""Explicit one-shot GPU bootstrap. Re-entry requires controller reconciliation.

Only local source bundle, pinned dependency artifact and pinned public model
files are accepted. No provider credentials, rental calls or job submissions.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import time
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen

MAX_SOURCE = 16 * 1024**2
MAX_DEPENDENCIES = 32 * 1024**3
MAX_UNPACKED = 80 * 1024**3
EXPECTED_FIELDS = {"version", "install_root", "source_bundle_path", "source_bundle_sha256",
    "dependency_artifact_url", "dependency_artifact_path", "dependency_artifact_sha256",
    "manifest_path", "model_root", "config_path", "status_path", "port", "prepared_root"}


def sha_file(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(8 * 1024**2), b""):
            value.update(chunk)
    return value.hexdigest()


def checked_path(value):
    path = Path(value)
    if not path.is_absolute():
        raise ValueError("absolute_path_required")
    current = Path(path.anchor)
    for part in path.parts[1:]:
        if part in {".", ".."}:
            raise ValueError("invalid_path")
        current /= part
        if current.is_symlink():
            raise ValueError("linked_path_refused")
    return path


def extract(archive, target, *, maximum):
    target.mkdir(parents=True, exist_ok=False)
    total, names = 0, set()
    with tarfile.open(archive, "r:gz") as source:
        for item in source:
            name = PurePosixPath(item.name)
            if (not item.isfile() or name.is_absolute() or not name.parts
                    or any(v in {".", ".."} for v in name.parts)
                    or "\\" in item.name or ":" in item.name or str(name) != item.name
                    or item.name in names):
                raise ValueError("unsafe_package_member")
            names.add(item.name)
            total += item.size
            if total > maximum or len(names) > 100000:
                raise ValueError("package_limit")
            output = target.joinpath(*name.parts)
            output.parent.mkdir(parents=True, exist_ok=True)
            with source.extractfile(item) as reader, output.open("xb") as writer:
                shutil.copyfileobj(reader, writer, 1024**2)
            output.chmod(0o644)
    return total


def validate_config(value):
    fields = EXPECTED_FIELDS | ({"deployment_profile_id", "profile_slot_index", "expected_host_gpus"}
        if isinstance(value, dict) and value.get("deployment_profile_id") else set())
    if not isinstance(value, dict) or set(value) != fields or value["version"] != 1:
        raise ValueError("bootstrap_configuration_invalid")
    if value.get("deployment_profile_id"):
        if (not isinstance(value["deployment_profile_id"], str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,120}", value["deployment_profile_id"])
                or type(value["expected_host_gpus"]) is not int or not 1 <= value["expected_host_gpus"] <= 8
                or type(value["profile_slot_index"]) is not int or not 0 <= value["profile_slot_index"] < value["expected_host_gpus"]
                or not value["prepared_root"] or value["dependency_artifact_url"] or value["dependency_artifact_path"]):
            raise ValueError("bootstrap_profile_configuration_invalid")
    for key in ("install_root", "source_bundle_path", "manifest_path", "model_root", "config_path", "status_path"):
        checked_path(value[key])
    if Path(value["config_path"]).name != "wgp_config.json":
        raise ValueError("runtime_configuration_filename_invalid")
    for key in ("source_bundle_sha256", "dependency_artifact_sha256"):
        if not isinstance(value[key], str) or not re.fullmatch("[0-9a-f]{64}", value[key]):
            raise ValueError("bootstrap_digest_required")
    remote, local = value["dependency_artifact_url"], value["dependency_artifact_path"]
    prepared = value["prepared_root"]
    if sum(bool(v) for v in (remote, local, prepared)) != 1:
        raise ValueError("one_dependency_source_required")
    if remote:
        parts = urlsplit(remote)
        if (parts.scheme != "https" or not parts.hostname or parts.username or parts.password
                or parts.query or parts.fragment):
            raise ValueError("unsigned_https_artifact_required")
    elif local:
        checked_path(local)
    else:
        checked_path(prepared)
    if type(value["port"]) is not int or not 1024 <= value["port"] <= 65535:
        raise ValueError("private_port_invalid")
    return value


def download(url, target, *, maximum, progress=None):
    class HTTPSRedirect(HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            parts = urlsplit(newurl)
            if parts.scheme != "https" or parts.username or parts.password:
                raise ValueError("download_transport_invalid")
            return super().redirect_request(req, fp, code, msg, headers, newurl)
    partial = target.with_name(target.name + ".part")
    size = 0
    with build_opener(HTTPSRedirect()).open(Request(url, headers={"User-Agent": "sixnine-pinned-bootstrap/1"}), timeout=60) as response:
        if urlsplit(response.url).scheme != "https":
            raise ValueError("download_transport_invalid")
        with partial.open("xb") as writer:
            while chunk := response.read(8 * 1024**2):
                size += len(chunk)
                if size > maximum:
                    raise ValueError("download_limit")
                writer.write(chunk)
                if progress:
                    progress(size)
            writer.flush()
            os.fsync(writer.fileno())
    partial.replace(target)


def install(config, slot_key, token_file, *, launch=True):
    config = validate_config(config)
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,200}", slot_key) or slot_key in {".", ".."}:
        raise ValueError("slot_key_invalid")
    token_file = checked_path(token_file)
    base = checked_path(config["install_root"])
    base.mkdir(parents=True, exist_ok=True)
    marker = base / "wangp-bootstrap-started.json"
    try:
        with marker.open("x", encoding="utf-8") as target:
            json.dump({"slot_key": slot_key, "started_unix": time.time()}, target)
    except FileExistsError:
        # Preserve the original process/status/journal; a repeated start is not recovery.
        return {"state": "reconcile_required", "code": "bootstrap_marker_exists"}
    marker.chmod(0o600)
    status_path = checked_path(config["status_path"])
    status_path.parent.mkdir(parents=True, exist_ok=True)
    started = False
    manifest_digest = None
    last_phase = "checking_package"
    phase_started_unix = time.time()
    started_unix = phase_started_unix
    last_updated_unix = started_unix
    download_progress = {}
    failure_diagnostics = {}

    def status(phase, **fields):
        nonlocal last_phase, phase_started_unix, last_updated_unix
        now = max(time.time(), last_updated_unix)
        last_updated_unix = now
        if phase != last_phase:
            phase_started_unix = now
        last_phase = phase
        for name in ("bytes_complete", "bytes_total", "files_complete", "files_total"):
            if name in fields:
                download_progress[name] = fields[name]
        value = {"phase": phase, "updated_unix": now, "phase_started_unix": phase_started_unix,
                 "started_unix": started_unix, "slot_key": slot_key,
                 "manifest_digest": manifest_digest, "engine_manifest_digest": manifest_digest,
                 "state": "booting", "inference_verified": False, **download_progress, **fields}
        temporary = status_path.with_suffix(status_path.suffix + ".tmp")
        with temporary.open("w", encoding="utf-8") as target:
            temporary.chmod(0o600)
            target.write(json.dumps(value, sort_keys=True))
            target.flush()
            os.fsync(target.fileno())
        temporary.replace(status_path)
        return value

    try:
        status("checking_package")
        if config.get("deployment_profile_id"):
            if platform.system() != "Linux" or platform.python_version() != "3.12.14":
                raise ValueError("native_profile_python31214_linux_required")
        elif platform.system() != "Linux" or not re.fullmatch(r"3\.11\.[0-9]+", platform.python_version()):
            raise ValueError("python311_linux_required")
        bundle = checked_path(config["source_bundle_path"])
        if bundle.stat().st_size > MAX_SOURCE or sha_file(bundle) != config["source_bundle_sha256"]:
            raise ValueError("source_bundle_mismatch")
        source = base / "platform"
        extract(bundle, source, maximum=MAX_SOURCE)
        sys.path.insert(0, str(source))
        from studio_platform.runtime_hosts.wangp_environment import (canonical, digest, validate_lock,
            verify_source, system_packages, regular_file, system_package_diagnostics)
        from studio_platform.inference.wangp_contract import EngineManifest
        from studio_platform.runtime_hosts.wangp_session import config_for_model_root
        manifest = EngineManifest.from_dict(json.loads(Path(config["manifest_path"]).read_text(encoding="utf-8")))
        manifest_digest = manifest.digest
        if config.get("deployment_profile_id"):
            from studio_platform.runtime_hosts.wangp_profile_bootstrap import prepare
            runtime, python, environment = prepare(config, manifest, source, status)
        else:
            if manifest.document.get("runtime_digest_kind") != "sixnine-environment-lock-sha256":
                raise ValueError("resolved_environment_manifest_required")
            if config["prepared_root"]:
                dependency = checked_path(config["prepared_root"]) / "dependencies"
            else:
                status("dependency_download")
                if config["dependency_artifact_path"]:
                    artifact = checked_path(config["dependency_artifact_path"])
                else:
                    artifact = base / "dependencies.tar.gz"
                    download(config["dependency_artifact_url"], artifact, maximum=MAX_DEPENDENCIES)
                if artifact.stat().st_size > MAX_DEPENDENCIES or sha_file(artifact) != config["dependency_artifact_sha256"]:
                    raise ValueError("dependency_artifact_mismatch")
                dependency = base / "dependencies"
                status("dependency_unpack")
                extract(artifact, dependency, maximum=MAX_UNPACKED)
            runtime = dependency / "upstream"
            lock = validate_lock(json.loads((runtime / ".sixnine-environment.json").read_text(encoding="utf-8")))
            if digest(lock) != manifest.document["runtime_digest"]:
                raise ValueError("environment_binding_mismatch")
            deb_files = []
            for item in lock.get("debs", []):
                deb = regular_file(dependency / "debs", item["file"])
                if deb.stat().st_size != item["size_bytes"] or sha_file(deb) != item["sha256"]:
                    raise ValueError("system_deb_mismatch")
                deb_files.append(str(deb))
            if deb_files:
                status("system_package_install")
                subprocess.run(["dpkg", "--install", *deb_files], check=True, capture_output=True,
                               env=dict(os.environ, DEBIAN_FRONTEND="noninteractive"))
            restore_kit = source / "system-restore"
            if restore_kit.exists():
                from studio_platform.runtime_hosts.wangp_system_restore import restore_system
                status("system_package_restore")
                try:
                    restore_system(restore_kit, lock)
                except ValueError as error:
                    if str(error) in {"system_restore_unrecognized_drift", "system_restore_verification_failed"}:
                        failure_diagnostics["system_package_diagnostics"] = system_package_diagnostics(
                            lock["system_packages"], system_packages())
                    raise
            status("system_package_verification")
            observed_system = system_packages()
            if any(observed_system.get(name) != version for name, version in lock["system_packages"].items()):
                failure_diagnostics["system_package_diagnostics"] = system_package_diagnostics(
                    lock["system_packages"], observed_system)
                raise ValueError("system_package_mismatch")
            verify_source(runtime, lock)
            requirements = dependency / "requirements.lock"
            if sha_file(requirements) != lock["requirements_lock_sha256"]:
                raise ValueError("requirements_lock_mismatch")
            for item in lock["wheels"]:
                wheel = regular_file(dependency / "wheels", item["file"])
                if wheel.stat().st_size != item["size_bytes"] or sha_file(wheel) != item["sha256"]:
                    raise ValueError("wheel_mismatch")
            venv = (checked_path(config["prepared_root"]) if config["prepared_root"] else base) / "venv"
            python = str(venv / "bin/python")
            environment = dict(os.environ, PYTHONPATH=str(source), HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1",
                               PIP_CONFIG_FILE=os.devnull, PYTHONDONTWRITEBYTECODE="1")
            if not config["prepared_root"]:
                status("dependency_install")
                subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True, capture_output=True)
                subprocess.run([python, "-m", "pip", "install", "--no-index", "--require-hashes", "--find-links",
                                str(dependency / "wheels"), "-r", str(requirements)], check=True, capture_output=True, env=environment)
            subprocess.run([python, "-m", "pip", "check"], check=True, capture_output=True, env=environment)
            status("runtime_imports")
            imported = subprocess.run([python, str(source / "deploy/wangp/probe_gpu.py"),
                                      "--runtime-root", str(runtime), "--output", str(base / "gpu-import.json")],
                                     capture_output=True, text=True, env=environment, timeout=180)
            if imported.returncode != 0:
                raise ValueError("runtime_import_probe_failed")
            import_evidence = json.loads(imported.stdout)
            if (import_evidence.get("state") != "imports_verified"
                    or import_evidence.get("environment_lock_sha256") != digest(lock)
                    or import_evidence.get("inference_verified") is not False):
                raise ValueError("runtime_import_receipt_mismatch")
        model_root = checked_path(config["model_root"])
        model_root.mkdir(parents=True, exist_ok=True)
        from studio_platform.runtime_hosts.wangp_download import DownloadFailure, run_download, safe_download_failure
        status("model_download")
        try:
            if config.get("deployment_profile_id"):
                from studio_platform.runtime_hosts.wangp_profile_bootstrap import download_profile
                download_profile(config, python, source, manifest_digest, model_root, base / "model-download", environment, status)
            else:
                run_download(python, source, config["manifest_path"], manifest_digest,
                             model_root, base / "model-download", environment, status)
        except DownloadFailure as error:
            failure_diagnostics["download_failure"] = safe_download_failure(error.diagnosis)
            raise ValueError(str(error)) from None
        runtime_config = checked_path(config["config_path"])
        runtime_config.parent.mkdir(parents=True, exist_ok=True)
        with runtime_config.open("x", encoding="utf-8") as target:
            if config.get("deployment_profile_id"):
                from studio_platform.runtime_catalog import runtime_config as profile_config
                json.dump(profile_config(config["deployment_profile_id"], model_root), target)
            else:
                json.dump(config_for_model_root(model_root), target)
        runtime_config.chmod(0o600)
        state = base / "slot-state"
        command = [python, "-m", "studio_platform.runtime_hosts.wangp_launcher", "--runtime-root", str(runtime),
            "--config", str(runtime_config), "--manifest", config["manifest_path"], "--model-root", str(model_root),
            "--state-dir", str(state), "--token-file", str(token_file), "--slot-key", slot_key, "--port", str(config["port"])]
        if not launch:
            status("runtime_verification")
            verified = subprocess.run(command + ["--verify-only"], check=True, capture_output=True, text=True, env=environment)
            evidence = json.loads(verified.stdout)
            if evidence.get("manifest_digest") != manifest_digest or evidence.get("inference_verified") is not False:
                raise ValueError("verification_receipt_mismatch")
            receipt_path = base / "runtime-verification.json"
            fd = os.open(receipt_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "wb") as target:
                target.write(canonical(evidence))
                target.flush()
                os.fsync(target.fileno())
            return status("runtime_files_verified", state="verified_not_started")
        # Normal startup verifies once inside the owned launcher, before Session
        # initialization/readiness. Its receipt never substitutes for a new hash.
        # GPU facts are observations, not an inferred resource guarantee.
        gpu = subprocess.run(["nvidia-smi", "--query-gpu=uuid,memory.total", "--format=csv,noheader,nounits"],
                             check=True, capture_output=True, text=True)
        devices = []
        for line in gpu.stdout.splitlines():
            uuid, memory = (v.strip() for v in line.split(","))
            if not re.fullmatch(r"GPU-[A-Za-z0-9-]+", uuid) or not memory.isdecimal():
                raise ValueError("gpu_observation_invalid")
            devices.append({"uuid": uuid, "total_bytes": int(memory) * 1024**2})
        if config.get("deployment_profile_id"):
            from studio_platform.runtime_hosts.wangp_profile_bootstrap import selected_devices
            devices = selected_devices(config, devices, expected_uuid=environment['CUDA_VISIBLE_DEVICES'])
        if len(devices) != 1:
            raise ValueError("single_gpu_recipe_required")
        with token_file.open("rb") as stream:
            token_info = os.fstat(stream.fileno())
            token_bytes = stream.read(513).strip()
        if (token_info.st_nlink != 1 or token_info.st_mode & 0o077
                or not 32 <= len(token_bytes) <= 512 or not token_bytes.isascii()
                or any(chr(c).isspace() for c in token_bytes)):
            raise ValueError("private_token_invalid")
        token = token_bytes.decode("ascii")
        status("runtime_start")
        from uuid import uuid4
        from studio_platform.runtime_hosts.wangp_startup import read_startup_failure
        startup_id = uuid4().hex
        startup_receipt = base / ("runtime-startup-" + startup_id + ".json")
        command += ["--startup-status-file", str(startup_receipt), "--startup-id", startup_id,
                    "--expected-manifest-digest", manifest_digest]
        # Popen failure is not proof that no child was ever created.
        started = True
        with open(os.devnull, "wb") as quiet:
            child = subprocess.Popen(command + ["--create-journal"], cwd=str(source), env=environment,
                stdin=subprocess.DEVNULL, stdout=quiet, stderr=quiet, start_new_session=True)
        from studio_platform.runtime_hosts.wangp_receipts import VERIFICATION_RECEIPT, read_verification_receipt
        deadline = time.monotonic() + 900
        while time.monotonic() < deadline:
            if child.poll() is not None:
                try:
                    failure = read_startup_failure(startup_receipt, slot_key=slot_key,
                        manifest_digest=manifest_digest, launch_id=startup_id, pid=child.pid)
                except Exception:
                    failure = None
                if failure is not None:
                    # A caught failure alone is not proof all runtime descendants
                    # stopped. With the child's session empty and no process on
                    # the GPU it is terminal, so the node can stop now (#871).
                    stopped = runtime_stopped(child.pid)
                    return status("setup_failed" if stopped else "runtime_start_unknown",
                        state="failed" if stopped else "unknown", failed_phase=failure["phase"],
                        failure_phase=failure["phase"], error_type=failure["error_type"],
                        code=failure["error_code"], runtime_verified=False)
                raise ValueError("runtime_process_exited")
            verified = None
            if (state / VERIFICATION_RECEIPT).exists():
                verified = read_verification_receipt(state, manifest_digest=manifest_digest,
                    slot_key=slot_key, pid=child.pid)
            try:
                request = Request(f"http://127.0.0.1:{config['port']}/v1/readiness",
                                  headers={"Authorization": "Bearer " + token})
                with urlopen(request, timeout=5) as response:
                    ready = json.loads(response.read(16385))
                if (verified is not None and ready.get("manifest_digest") == manifest_digest
                        and ready.get("slot_key") == slot_key and ready.get("incarnation") == verified["incarnation"]
                        and ready.get("idle") is True):
                    return status("runtime_ready", state="ready", pid=child.pid, runtime_verified=True,
                                  source_revision=manifest.document["source_revision"],
                                  gpus=devices, runtime={"gpu_total_bytes": devices[0]["total_bytes"]},
                                  port=config["port"], readiness_path="/v1/readiness")
            except Exception:
                pass
            status("runtime_start" if verified else "runtime_verification", pid=child.pid)
            time.sleep(2)
        raise ValueError("runtime_readiness_timeout")
    except Exception as error:
        allowed = isinstance(error, ValueError) and re.fullmatch(r"[a-z0-9_]{1,100}", str(error))
        error_type = type(error).__name__ if type(error) in (ValueError, TypeError, OSError, FileNotFoundError,
            PermissionError, TimeoutError, subprocess.CalledProcessError, subprocess.TimeoutExpired) else "SetupError"
        return status("runtime_start_unknown" if started else "setup_failed", state="unknown" if started else "failed",
                      failed_phase=last_phase, failure_phase=last_phase, error_type=error_type,
                      code=str(error) if allowed else "bootstrap_operation_failed", **failure_diagnostics)


def runtime_stopped(session, wait=10):
    """No process left in the launcher's own session and none on the GPU."""
    deadline = time.monotonic() + wait
    while True:
        try:
            os.killpg(session, 0)
        except ProcessLookupError:
            try:
                apps = subprocess.run(["nvidia-smi", "--query-compute-apps=pid", "--format=csv,noheader"],
                    capture_output=True, text=True, timeout=30, check=True).stdout.strip()
            except Exception:
                return False
            if not apps:
                return True
        except OSError:
            return False
        if time.monotonic() >= deadline:
            return False
        time.sleep(1)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--slot-key", required=True)
    parser.add_argument("--token-file", required=True)
    args = parser.parse_args(argv)
    try:
        value = install(json.loads(checked_path(args.config).read_text(encoding="utf-8")), args.slot_key, args.token_file)
    except Exception:
        value = {"state": "failed", "code": "bootstrap_configuration_invalid"}
    print(json.dumps(value))
    return 0 if value.get("state") in {"ready", "verified_not_started"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
