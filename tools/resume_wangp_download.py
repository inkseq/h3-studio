"""Explicit root-approved continuation of one stopped native model download.

This executable is staged separately: it never replaces the original bootstrap,
source bundle, manifest, token or markers. No provider, ledger or job operation.
The original bootstrap's exact remaining setup is used only once. Unknown or
repeated continuation intents require reconciliation, never automatic replay.
"""
from __future__ import annotations

import argparse
import ast
import errno
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import platform
import re
import socket
import stat
import subprocess
import sys
import tarfile
import threading
import time
import uuid

ORIGINAL_BOOTSTRAP_SHA = "2620c95b0ea33b83e2e1d52949d6f2ba68965b9429b5620d56502d39323fafa6"
INPUTS = {"config", "token", "package", "manifest", "bootstrap", "identity", "marker", "status", "failure"}
SAFE_TYPES = {"HfHubHTTPError", "EntryNotFoundError", "RevisionNotFoundError", "RepositoryNotFoundError",
              "GatedRepoError", "LocalEntryNotFoundError", "HTTPError", "ConnectionError", "ReadTimeout",
              "ConnectTimeout", "TimeoutError", "OSError", "PermissionError", "FileNotFoundError", "RuntimeError"}


def require(value, code):
    if not value:
        raise ValueError("download_resume_" + code)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def parents(path, *, expected=None, extracted_dirs=frozenset()):
    for item in (path, *path.parents):
        info = item.lstat()
        require(stat.S_ISDIR(info.st_mode) and info.st_uid == 0, "parent_uncontrolled")
        if expected is not None and str(item) in expected:
            require({"uid": info.st_uid, "gid": info.st_gid, "mode": stat.S_IMODE(info.st_mode),
                     "is_directory": True} == expected[str(item)], "parent_metadata_changed")
        elif item in extracted_dirs:
            require(info.st_gid == 0 and stat.S_IMODE(info.st_mode) in {0o755, 0o775}, "parent_uncontrolled")
        else:
            require(not info.st_mode & 0o022, "parent_uncontrolled")


def approved_parents(authority):
    values = authority.get("parent_metadata")
    require(type(values) is dict and 1 <= len(values) <= 32, "parent_authority_invalid")
    for name, metadata in values.items():
        path = Path(name)
        require(path.is_absolute() and str(path) == name and ".." not in path.parts
                and type(metadata) is dict and type(metadata.get("uid")) is int
                and type(metadata.get("gid")) is int and type(metadata.get("mode")) is int
                and metadata.get("is_directory") is True
                and metadata in ({"uid": 0, "gid": 0, "mode": mode, "is_directory": True}
                                 for mode in (0o700, 0o755, 0o775)), "parent_authority_invalid")
    return values


def read(path, maximum=32768, *, identity_marker=False, expected_parents=None, extracted_dirs=frozenset()):
    path = Path(path)
    require(path.is_absolute() and ".." not in path.parts, "path_invalid")
    parents(path.parent, expected=expected_parents, extracted_dirs=extracted_dirs)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(descriptor)
        historical_identity = (identity_marker and path.name == "sixnine-bootstrap-identity.json"
                               and info.st_gid == 0 and stat.S_IMODE(info.st_mode) == 0o664)
        require(stat.S_ISREG(info.st_mode) and info.st_uid == 0 and info.st_nlink == 1
                and (not info.st_mode & 0o022 or historical_identity)
                and 0 <= info.st_size <= maximum, "file_uncontrolled")
        raw = os.read(descriptor, maximum + 1)
        require(len(raw) == info.st_size, "file_changed")
        return raw
    finally:
        os.close(descriptor)


def save(path, value, *, exclusive=False):
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode() + b"\n"
    temporary = path if exclusive else path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(raw); stream.flush(); os.fsync(stream.fileno())
    if not exclusive:
        os.replace(temporary, path)
    descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def process_idle(port, *, proc=Path("/proc"), self_pid=None):
    """Reject incomplete/unstable visibility; never print process command lines."""
    self_pid = os.getpid() if self_pid is None else self_pid
    def listed():
        result = {}
        for item in proc.iterdir():
            if not item.name.isdecimal():
                continue
            with (item / "stat").open("rb") as stream:
                raw = stream.read(8193)
            fields = raw.rsplit(b")", 1)[-1].split()
            require(len(raw) <= 8192 and len(fields) >= 20 and fields[19].isdigit(), "process_visibility_invalid")
            result[item.name] = fields[19]  # Kernel start ticks, not PID alone.
        require(0 < len(result) <= 4096, "process_visibility_invalid")
        return result
    before = listed()
    for pid in before:
        if int(pid) == self_pid:
            continue
        with (proc / pid / "cmdline").open("rb") as stream:
            raw = stream.read(65537)
        require(len(raw) <= 65536, "process_visibility_invalid")
        args = raw.split(b"\0")
        require(not any(value == b"wangp-bootstrap.py" or value.endswith(b"/wangp-bootstrap.py")
                        or value in {b"studio_platform.runtime_hosts.wangp_download",
                                     b"studio_platform.runtime_hosts.wangp_launcher", b"--download-child",
                                     b"--startup-status-file"} for value in args), "live_process")
    require(listed() == before, "process_visibility_changed")
    with socket.socket() as client:
        client.settimeout(1)
        require(client.connect_ex(("127.0.0.1", port)) == errno.ECONNREFUSED, "runtime_port_listening")


def source_matches(package, source, *, expected_parents=None):
    names, total = set(), 0
    with tarfile.open(package, "r:gz") as archive:
        for member in archive:
            name = PurePosixPath(member.name)
            total += member.size
            require(member.isfile() and name.parts and not name.is_absolute() and str(name) == member.name
                    and not any(part in {".", ".."} for part in name.parts)
                    and "\\" not in member.name and member.name not in names
                    and 0 <= member.size <= total <= 16 * 1024**2, "source_member_invalid")
            names.add(member.name)
            require(len(names) <= 256, "source_member_invalid")
            data = archive.extractfile(member).read(member.size + 1)
            directories = {source, *(source / parent for parent in name.parents if str(parent) != ".")}
            require(len(data) == member.size and read(source / member.name, 16 * 1024**2,
                    expected_parents=expected_parents, extracted_dirs=directories) == data,
                    "extracted_source_changed")
    require(bool(names), "source_member_invalid")
    for path in source.rglob("*"):
        require(not path.is_symlink(), "extracted_source_changed")
        if path.is_file():
            relative = path.relative_to(source)
            require(relative.as_posix() in names or ("__pycache__" in relative.parts and path.suffix == ".pyc"),
                    "extracted_source_changed")
            if relative.as_posix() not in names:
                directories = {source, *(source / parent for name in names for parent in PurePosixPath(name).parents
                    if str(parent) != ".")}
                require(path.parent.name == "__pycache__" and path.parent.parent in directories,
                        "extracted_source_changed")
                read(path, 16 * 1024**2, expected_parents=expected_parents,
                     extracted_dirs=directories | {path.parent})


def original_module(raw):
    require(sha(raw) == ORIGINAL_BOOTSTRAP_SHA, "bootstrap_version_unsupported")
    # Executing a bounded, approved source byte string does not write pyc or
    # import a different local bootstrap via incidental module search paths.
    import types
    module = types.ModuleType("original_native_bootstrap")
    exec(compile(raw, "sha-pinned-original-bootstrap", "exec"), module.__dict__)
    return module


def paths_for(config, config_path, token_file):
    base = Path(config["install_root"])
    remote = Path(config["status_path"]).parent
    return {"config": Path(config_path), "token": Path(token_file),
            "package": Path(config["source_bundle_path"]), "manifest": Path(config["manifest_path"]),
            "bootstrap": remote / "wangp-bootstrap.py", "identity": remote / "sixnine-bootstrap-identity.json",
            "marker": base / "wangp-bootstrap-started.json", "status": Path(config["status_path"]),
            "failure": base / "model-download/failure.json"}


def read_input(name, path, expected_parents=None):
    # The original nonsecret identity marker was created root:root 0664. Its
    # independently approved exact bytes remain pinned; no chmod or broad
    # relaxation of secret, source, authority, or journal readers is permitted.
    return read(path, 16 * 1024**2 if name == "package" else 65536,
                identity_marker=name == "identity", expected_parents=expected_parents)


def validate(authority, config, paths, raw, *, now):
    keys = {"version", "purpose", "resume_id", "slot_key", "run_id", "instance_id",
            "hard_deadline", "issued_at", "expires_at", "input_sha256", "recovery_sha256", "parent_metadata"}
    version = authority.get("version")
    require(type(version) is int and version in {1, 2}
            and set(authority) == (keys if version == 1 else keys | {"previous_intent_sha256", "download_alias"})
            and authority["purpose"] == "resume-original-model-download-v" + str(version),
            "authority_invalid")
    if version == 2:
        require(isinstance(authority["previous_intent_sha256"], str)
                and re.fullmatch(r"[0-9a-f]{64}", authority["previous_intent_sha256"]), "authority_invalid")
    require(re.fullmatch(r"[0-9a-f]{32}", authority["resume_id"]) is not None
            and set(authority["input_sha256"]) == INPUTS, "authority_invalid")
    for value in (authority["hard_deadline"], authority["issued_at"], authority["expires_at"]):
        require(type(value) in (int, float) and math.isfinite(value), "authority_invalid")
    require(0 <= now - authority["issued_at"] <= 300 and now < authority["expires_at"] <= authority["issued_at"] + 300
            <= authority["hard_deadline"] and authority["hard_deadline"] - now > 900, "authority_expired")
    require(authority["recovery_sha256"] == sha(read(Path(__file__), 65536)), "recovery_changed")
    require(all(sha(raw[name]) == authority["input_sha256"][name] for name in INPUTS), "input_changed")
    require(sha(raw["bootstrap"]) == ORIGINAL_BOOTSTRAP_SHA, "bootstrap_version_unsupported")
    original_module(raw["bootstrap"]).validate_config(config)
    expected_parents = approved_parents(authority)
    bases = (Path(config["install_root"]), Path(config["status_path"]).parent)
    require(set(expected_parents) == {str(item) for base in bases for item in (base, *base.parents)},
            "parent_authority_invalid")
    require(config.get("deployment_profile_id") and config.get("expected_host_gpus") == 1
            and config.get("profile_slot_index") == 0 and config.get("prepared_root")
            and not config.get("dependency_artifact_url") and not config.get("dependency_artifact_path"),
            "prepared_profile_required")
    identity, marker, status_value, failure = (json.loads(raw[name]) for name in ("identity", "marker", "status", "failure"))
    sources = identity.get("sources", {})
    require(identity.get("intent_id") == marker.get("slot_key") == status_value.get("slot_key") == authority["slot_key"]
            and identity.get("dstack_run_id") == authority["run_id"]
            and identity.get("instance_id") == authority["instance_id"]
            and identity.get("deployment_profile_id") == config["deployment_profile_id"]
            and identity.get("profile_slot_index") == 0 and identity.get("expected_host_gpus") == 1
            and sources == {"wangp-bootstrap.py": sha(raw["bootstrap"]), "wangp-runtime.json": sha(raw["config"]),
                            "wangp-package.tar.gz": sha(raw["package"]), "wangp-manifest.json": sha(raw["manifest"])}
            and config["source_bundle_sha256"] == sha(raw["package"]), "original_identity_changed")
    manifest = identity.get("engine_manifest_digest")
    diagnosis = failure.get("download_failure")
    require(status_value.get("state") == "failed" and status_value.get("failure_phase") == "model_download"
            and status_value.get("manifest_digest") == manifest == failure.get("manifest_digest")
            and failure.get("state") == "failed" and type(diagnosis) is dict
            and diagnosis == {"error_code": failure.get("error_code"), "stop_status": "confirmed"}
            and diagnosis["error_code"] in {"model_download_failed", "model_download_timeout"}
            and status_value.get("download_failure") == diagnosis, "stopped_failure_required")
    base = Path(config["install_root"])
    forbidden = (Path(config["config_path"]), base / "slot-state")
    require(not any(path.exists() or path.is_symlink() for path in forbidden)
            and not list(base.glob("runtime-startup-*.json")), "runtime_already_started")
    source_matches(paths["package"], base / "platform", expected_parents=expected_parents)
    if version == 2:
        # A separately staged recovery executable has no checkout on sys.path.
        # Resolve the pure manifest contract only from the original source whose
        # archive and extracted bytes were verified above.
        sys.path.insert(0, str(base / "platform"))
        validate_alias(authority["download_alias"], json.loads(raw["manifest"]))
    process_idle(config["port"])
    return identity


def validate_alias(alias, document):
    """An explicit byte-preserving locator, never a different model manifest."""
    from studio_platform.runtime_hosts.wangp_download import selected_files
    from studio_platform.inference.wangp_contract import EngineManifest
    require(type(alias) is dict and set(alias) == {"repository", "original_revision", "download_revision",
            "files", "evidence_sha256"}, "download_alias_invalid")
    require(all(isinstance(alias[key], str) and re.fullmatch(r"[0-9a-f]{40}", alias[key])
                for key in ("original_revision", "download_revision"))
            and alias["original_revision"] != alias["download_revision"]
            and isinstance(alias["evidence_sha256"], str)
            and re.fullmatch(r"[0-9a-f]{64}", alias["evidence_sha256"]), "download_alias_invalid")
    manifest = EngineManifest.from_dict(document)
    records = selected_files(document, manifest.digest)
    require(all(row[1] == alias["repository"] and row[2] == alias["original_revision"] for row in records),
            "download_alias_binding_changed")
    original = [item for component in document["components"].values() for item in component["files"]]
    require(type(alias["files"]) is list and len(alias["files"]) == len(original)
            and all(type(item) is dict for item in alias["files"])
            and sorted(alias["files"], key=lambda item: item.get("path", ""))
                == sorted(original, key=lambda item: item["path"]), "download_alias_content_changed")
    return alias


def alias_arguments(arguments, alias):
    require(arguments.get("repo_id") == alias["repository"]
            and arguments.get("revision") == alias["original_revision"]
            and arguments.get("filename") in {item["path"] for item in alias["files"]}
            and arguments.get("token") is False and arguments.get("endpoint") == "https://huggingface.co",
            "download_alias_binding_changed")
    return {**arguments, "revision": alias["download_revision"]}


def previous_failed_intent(raw, authority):
    require(sha(raw) == authority.get("previous_intent_sha256"), "previous_intent_changed")
    previous = json.loads(raw)
    result = previous.get("result", {})
    require(previous.get("status") == "observed_result" and type(result) is dict
            and result.get("state") == "failed" and result.get("failure_phase") == "model_download"
            and result.get("code") in {"model_download_failed", "model_download_timeout"}
            and result.get("download_failure") == {"error_code": result.get("code"), "stop_status": "confirmed"}
            and previous.get("slot_key") == authority["slot_key"]
            and previous.get("hard_deadline") == authority["hard_deadline"]
            and type(previous.get("input_sha256")) is dict
            and all(previous["input_sha256"].get(name) == authority["input_sha256"].get(name)
                    for name in INPUTS - {"status"}), "previous_intent_not_terminal")


def original_tail(module, raw):
    """Reuse the SHA-pinned original status/launcher/verifier, not a forked runtime."""
    require(sha(raw) == ORIGINAL_BOOTSTRAP_SHA, "bootstrap_version_unsupported")
    tree = ast.parse(raw)
    install = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "install")
    status_node = next(node for node in install.body if isinstance(node, ast.FunctionDef) and node.name == "status")
    body = next(node for node in install.body if isinstance(node, ast.Try) and node.handlers
                and len(node.body) > 10)
    start = next(index for index, node in enumerate(body.body) if isinstance(node, ast.Assign)
                 and isinstance(node.targets[0], ast.Name) and node.targets[0].id == "runtime_config")
    arguments = "config,slot_key,token_file,base,source,runtime,manifest,manifest_digest,python,environment,download_once"
    wrapper = ast.parse("def continue_original(" + arguments + "):\n"
                        " started=False\n last_phase='model_download'\n failure_diagnostics={}\n launch=True\n"
                        " status_path=Path(config['status_path'])\n model_root=Path(config['model_root'])\n pass\n").body[0]
    download = ast.parse("try:\n download_once(status)\nexcept DownloadFailure as error:\n"
                         " failure_diagnostics['download_failure']=safe_download_failure(error.diagnosis)\n"
                         " raise ValueError(str(error)) from None\n").body[0]
    wrapper.body[-1:] = [status_node, ast.Try(body=[download, *body.body[start:]],
                                            handlers=body.handlers, orelse=[], finalbody=[])]
    code = ast.fix_missing_locations(ast.Module(body=[wrapper], type_ignores=[]))
    namespace = dict(module.__dict__)
    from studio_platform.runtime_hosts.wangp_download import DownloadFailure, safe_download_failure
    namespace.update(DownloadFailure=DownloadFailure, safe_download_failure=safe_download_failure)
    exec(compile(code, "sha-pinned-original-setup-tail", "exec"), namespace)
    return namespace["continue_original"]


def classification(error):
    name = type(error).__name__
    try:
        status_code = getattr(getattr(error, "response", None), "status_code", None)
    except Exception:
        status_code = None
    return {"exception_type": name if name in SAFE_TYPES else "OtherDownloadError",
            "http_status": status_code if type(status_code) is int and 100 <= status_code <= 599 else None}


def download_child(argv):
    """Same pinned SDK/options; only a closed extra diagnosis in a new attempt."""
    alias = None
    if argv[:1] == ["--download-alias-file"]:
        require(len(argv) >= 3, "download_alias_invalid")
        alias = json.loads(read(Path(argv[1])))
        argv = argv[2:]
    from studio_platform.runtime_hosts import wangp_download as old
    original = old.fetch_files
    lock = threading.Lock()
    def fetch(document, digest, root, progress, **kwargs):
        if alias is not None:
            validate_alias(alias, document)
        from huggingface_hub import hf_hub_download
        def downloader(**arguments):
            try:
                return hf_hub_download(**(alias_arguments(arguments, alias) if alias is not None else arguments))
            except Exception as error:
                with lock:
                    path = Path(progress).parent / "sdk-failure.json"
                    if not path.exists():
                        try:
                            save(path, classification(error), exclusive=True)
                        except Exception:
                            pass  # Diagnosis cannot replace the original SDK failure/stop guard.
                raise
        return original(document, digest, root, progress, downloader=downloader, **kwargs)
    old.fetch_files = fetch  # This isolated child only; no installed source modification.
    return old.main(argv)


def execute(config, paths, authority, attempt):
    expected_parents = approved_parents(authority)
    source = Path(config["install_root"]) / "platform"
    sys.path.insert(0, str(source))
    module = original_module(read_input("bootstrap", paths["bootstrap"], expected_parents))
    module.validate_config(config)
    from studio_platform.inference.wangp_contract import EngineManifest
    from studio_platform.runtime_hosts.wangp_profile_bootstrap import prepare, _acquire_cache_lock
    from studio_platform.runtime_hosts.wangp_download import run_download
    manifest = EngineManifest.from_dict(json.loads(read_input("manifest", paths["manifest"], expected_parents)))
    require(manifest.digest == json.loads(read_input("identity", paths["identity"], expected_parents))["engine_manifest_digest"], "manifest_changed")
    runtime, python, environment = prepare(config, manifest, source, lambda *args, **kwargs: None)
    alias_path = None
    if authority["version"] == 2:
        validate_alias(authority["download_alias"], manifest.document)
        alias_path = Path(__file__).parent / ("download-alias-" + authority["resume_id"] + ".json")
        save(alias_path, authority["download_alias"], exclusive=True)
    def download_once(status):
        lock = Path(config["model_root"]) / ".sixnine-profile-download.lock"
        require(not lock.is_symlink(), "cache_lock_invalid")
        descriptor = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "a+b") as stream:
            info = os.fstat(stream.fileno())
            require(stat.S_ISREG(info.st_mode) and info.st_uid == 0 and info.st_nlink == 1, "cache_lock_invalid")
            timeout = min(1800, authority["hard_deadline"] - time.time() - 901)
            require(timeout > 0, "authority_expired")
            _acquire_cache_lock(stream, status, timeout=timeout)
            timeout = min(7200, authority["hard_deadline"] - time.time() - 900)
            require(timeout >= 1, "authority_expired")
            def popen(command, **kwargs):
                alias_args = ["--download-alias-file", str(alias_path)] if alias_path is not None else []
                return subprocess.Popen([command[0], str(Path(__file__).resolve()), "--download-child",
                    *alias_args, *command[3:]], **kwargs)
            run_download(python, source, paths["manifest"], manifest.digest, config["model_root"],
                         attempt, environment, status, timeout=timeout, popen=popen)
        require(time.time() < authority["hard_deadline"] - 900, "authority_expired")
    return original_tail(module, read_input("bootstrap", paths["bootstrap"], expected_parents))(config, authority["slot_key"], paths["token"],
        Path(config["install_root"]), source, runtime, manifest, manifest.digest, python, environment, download_once)


def resume(config_path, token_file, authority_path, *, effect=execute):
    require(sys.platform == "linux" and os.getuid() == 0, "linux_root_required")
    require(platform.python_version() == "3.12.14", "original_python_required")
    authority_raw = read(authority_path)
    authority = json.loads(authority_raw)
    expected_parents = approved_parents(authority)
    config_raw = read(config_path, expected_parents=expected_parents)
    config = json.loads(config_raw)
    paths = paths_for(config, config_path, token_file)
    base = Path(config["install_root"])
    parents(base, expected=expected_parents)
    import fcntl
    descriptor = os.open(base / "resume-original-download.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "a+b") as lock:
        info = os.fstat(lock.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_uid == 0 and info.st_nlink == 1
                and not info.st_mode & 0o077, "resume_lock_invalid")
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        previous = list(base.glob("resume-original-download-*.json"))
        if previous:
            require(len(previous) <= 2, "intent_requires_reconciliation")
            prior_raw = [read(path, expected_parents=expected_parents) for path in previous]
            if any(json.loads(value).get("authority_sha256") == sha(authority_raw) for value in prior_raw):
                return {"state": "reconcile_required", "code": "download_resume_original_intent_exists"}
            require(authority.get("version") == 2, "intent_changed")
            require(len(previous) == 1, "intent_requires_reconciliation")
            require(previous[0].name != "resume-original-download-" + authority["resume_id"] + ".json", "intent_changed")
            previous_failed_intent(prior_raw[0], authority)
        else:
            require(authority.get("version") != 2, "previous_intent_missing")
        raw = {name: read_input(name, path, expected_parents) for name, path in paths.items()}
        validate(authority, config, paths, raw, now=time.time())
        journal_path = base / ("resume-original-download-" + authority["resume_id"] + ".json")
        journal = {"authority_sha256": sha(authority_raw), "slot_key": authority["slot_key"],
                   "input_sha256": authority["input_sha256"], "hard_deadline": authority["hard_deadline"],
                   "status": "continuation_started", "started_at": time.time()}
        save(journal_path, journal, exclusive=True)
        evidence = base / ("resume-original-download-" + authority["resume_id"])
        evidence.mkdir(mode=0o700)
        save(evidence / "original-status.json", json.loads(raw["status"]), exclusive=True)
        save(evidence / "original-failure.json", json.loads(raw["failure"]), exclusive=True)
        attempt = evidence / "download-attempt"
        result = effect(config, paths, authority, attempt)
        require(all(read_input(name, paths[name], expected_parents) == raw[name]
                    for name in INPUTS - {"status"}), "retained_input_changed")
        journal.update(status="observed_result", result=result, completed_at=time.time())
        save(journal_path, journal)
        return result


def main(argv=None):
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments and arguments[0] == "--download-child":
        return download_child(arguments[1:])
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--token-file", required=True, type=Path)
    parser.add_argument("--receipt", required=True, type=Path)
    args = parser.parse_args(arguments)
    try:
        result = resume(args.config, args.token_file, args.receipt)
        print(json.dumps(result, separators=(",", ":")))
        return 0 if result.get("state") == "ready" else 1
    except Exception as error:
        code = str(error) if type(error) is ValueError and str(error).startswith("download_resume_") else "download_resume_unconfirmed"
        print(json.dumps({"state": "reconcile_required", "code": code, "provider_operations": 0}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
