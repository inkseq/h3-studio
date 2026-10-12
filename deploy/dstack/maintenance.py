#!/usr/bin/python3
"""Root-installed additive migration release helper; never execute incoming code.

Install as /opt/sixnine-release/migration_maintenance.py after independent review,
with the reviewed checker as migration_check.py. Only the CPU Compose services
are controlled here. Provider shutdown, rentals and business state are untouched.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import shutil
import stat
import tarfile
import tempfile
import time

import release

FILES = frozenset({"dispatch-image.tar.gz", "platform-overlay.yaml", "hatchet-entrypoint.sh",
    "source-set.tar.gz", "migration-check.py", "maintenance.py", "runtime-config.example.json",
    "server-config.example.yml", "broker.example.json"})
MANIFEST = "migration-manifest.json"
SHARED = ("dstack", "hatchet", "dispatch", "controller")
PATH_FIELDS = frozenset({"SIXNINE_EXECUTION_PROFILES_SECRET_FILE", "SIXNINE_GRAFANA_CONFIG_FILE",
    "SIXNINE_DSTACK_OPERATOR_CONFIG_FILE", "SIXNINE_DSTACK_RUNTIME_CONFIG_FILE",
    "SIXNINE_DSTACK_TOKEN_FILE", "SIXNINE_DSTACK_DSN_FILE", "SIXNINE_DSTACK_SSH_PUBLIC_KEY_FILE",
    "SIXNINE_GPU_SSH_KEY_FILE", "SIXNINE_HATCHET_BROKER_CONFIG_FILE", "SIXNINE_HATCHET_TOKEN_FILE",
    "SIXNINE_HATCHET_SECRET_DIR", "SIXNINE_DSTACK_STATE_DIR", "SIXNINE_DSTACK_SERVER_CONFIG_FILE",
    "SIXNINE_DSTACK_RUNTIME_DIR"})
OTHER_FIELDS = frozenset({"SIXNINE_OPERATOR_CAPACITY_OWNERS", "SIXNINE_HATCHET_POSTGRES_VOLUME",
    "SIXNINE_HATCHET_CONFIG_VOLUME", "SIXNINE_MIGRATION_GENERATION_ENABLED",
    "SIXNINE_MIGRATION_EXECUTION_BACKEND", "SIXNINE_DEFAULT_DEPLOYMENT_PROFILE_ID"})
OPTIONAL_PATH_FIELDS = frozenset({"SIXNINE_STAGE_QUERY_CONFIG_FILE", "SIXNINE_STORAGE_CREDENTIAL_FILE"})
STORAGE_FIELDS = frozenset({"SIXNINE_STORAGE_PROVIDER", "SIXNINE_STORAGE_ENDPOINT", "SIXNINE_STORAGE_REGION",
    "SIXNINE_STORAGE_BUCKET", "SIXNINE_STORAGE_PROFILE", "SIXNINE_STORAGE_CREDENTIAL_FILE"})
OPTIONAL_FIELDS = frozenset({"SIXNINE_OPERATOR_ADMIN_OWNERS"}) | OPTIONAL_PATH_FIELDS | STORAGE_FIELDS
LEGACY_MARKERS = ("operator-capacity/active.json", "gpu-acceptance/active.json", "gpu-scaler/active.json")


def atomic(path, document):
    """Protected local release metadata only, never a task/lease authority."""
    path = Path(path)
    if path.exists() or path.is_symlink():
        release.regular(path, root_owned=True, maximum=262144)
    descriptor, temporary = tempfile.mkstemp(prefix=".migration-", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(document, output, sort_keys=True)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        release.sync_directory(path.parent)
    finally:
        Path(temporary).unlink(missing_ok=True)


def protected_directory(path):
    info = Path(path).lstat()
    release.require(stat.S_ISDIR(info.st_mode) and (os.name == "nt" or
        info.st_uid == 0 and not info.st_mode & 0o022), "migration_directory_not_root_controlled")


def approved(root, directory, commit):
    approval = root / "approved-migrations" / (commit + ".sha256")
    protected_directory(approval.parent)
    release.regular(approval, root_owned=True, maximum=128)
    digest = approval.read_text(encoding="ascii").strip()
    release.regular(directory / MANIFEST, maximum=32768)
    release.require(release.DIGEST.fullmatch(digest) and digest == release.checksum(directory / MANIFEST),
                    "migration_independent_approval_missing")


def manifest(directory, commit, *, platform_manifest_sha256=None):
    directory = Path(directory)
    release.require(release.SHA.fullmatch(commit) and directory.is_dir() and not directory.is_symlink(),
                    "migration_artifact_invalid")
    release.require({p.name for p in directory.iterdir()} == FILES | {MANIFEST}, "migration_artifact_files_invalid")
    release.regular(directory / MANIFEST, maximum=32768)
    value = json.loads((directory / MANIFEST).read_text(encoding="utf-8"))
    release.require(type(value) is dict and set(value) == {"version", "commit", "platform_manifest_sha256",
        "dispatch", "source_index_sha256", "files"} and type(value["version"]) is int and value["version"] == 1 and value["commit"] == commit
        and type(value.get("platform_manifest_sha256")) is str and release.DIGEST.fullmatch(value["platform_manifest_sha256"])
        and type(value.get("source_index_sha256")) is str and release.DIGEST.fullmatch(value["source_index_sha256"])
        and type(value["files"]) is dict and set(value["files"]) == FILES, "migration_manifest_invalid")
    if platform_manifest_sha256 is not None:
        release.require(value["platform_manifest_sha256"] == platform_manifest_sha256,
                        "migration_platform_binding_changed")
    dispatch = value["dispatch"]
    release.require(type(dispatch) is dict and set(dispatch) == {"image", "image_id"}
        and dispatch["image"] == "sixnine-dispatch:" + commit and type(dispatch["image_id"]) is str
        and re.fullmatch(r"sha256:[0-9a-f]{64}", dispatch["image_id"]), "migration_dispatch_binding_invalid")
    for name, digest in value["files"].items():
        release.require(type(digest) is str and release.DIGEST.fullmatch(digest), "migration_checksum_invalid")
        maximum = 2 * 1024**3 if name == "dispatch-image.tar.gz" else 64 * 1024**2 if name == "source-set.tar.gz" else 1024**2
        release.regular(directory / name, maximum=maximum)
        release.require(release.checksum(directory / name) == digest, "migration_checksum_mismatch")
    return value


def fetch(root, commit, *, api=None):
    """Fetch separately approved additive objects; never approve or start them."""
    release.require(release.SHA.fullmatch(commit), "invalid_commit")
    protected_directory(root / "incoming-migrations")
    if api is None:
        import boto3
        from botocore.config import Config
        api = boto3.client("s3", region_name="ap-southeast-1", endpoint_url="https://s3.ap-southeast-1.amazonaws.com",
            config=Config(connect_timeout=5, read_timeout=60, retries={"mode": "standard", "max_attempts": 3}))
    import fetch_release_s3
    staging = Path(tempfile.mkdtemp(prefix=".migration-download-", dir=root))
    target = root / "incoming-migrations" / commit
    try:
        prefix = "migration/"
        fetch_release_s3.download(api, commit, prefix + MANIFEST, staging / MANIFEST, 32768)
        approved(root, staging, commit)  # Before transferring the large image.
        for name in sorted(FILES):
            maximum = 2 * 1024**3 if name == "dispatch-image.tar.gz" else 64 * 1024**2 if name == "source-set.tar.gz" else 1024**2
            fetch_release_s3.download(api, commit, prefix + name, staging / name, maximum)
        value = manifest(staging, commit)
        approved(root, staging, commit)
        if target.exists() or target.is_symlink():
            release.require(manifest(target, commit) == value, "migration_existing_download_differs")
            approved(root, target, commit)
            return target
        staging.rename(target)
        release.sync_directory(target.parent)
        return target
    finally:
        if staging.exists():
            for name in FILES | {MANIFEST}:
                (staging / name).unlink(missing_ok=True)
            staging.rmdir()


def extract_sources(archive_path, target, index_digest):
    """Extract only the small public four-file source sets; no tar links/devices."""
    target = Path(target)
    release.require(not target.exists() and not target.is_symlink(), "migration_sources_already_exist")
    target.mkdir(mode=0o700)
    seen, total = set(), 0
    with tarfile.open(archive_path, "r|gz") as archive:
        for member in archive:
            name = member.name.rstrip("/")
            release.require(name and name not in seen and not name.startswith("/") and "\\" not in name
                and all(p not in {"", ".", ".."} for p in name.split("/")) and member.isfile(),
                "migration_source_archive_unsafe")
            seen.add(name); total += member.size
            release.require(len(seen) <= 1024 and total <= 256 * 1024**2 and member.size <= 64 * 1024**2,
                            "migration_source_archive_limit")
            destination = target / name
            destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with destination.open("xb") as output:
                shutil.copyfileobj(archive.extractfile(member), output, 1024 * 1024)
                output.flush(); os.fsync(output.fileno())
            destination.chmod(0o600)
    verify_sources(target, index_digest, seen=seen)
    if os.name == "posix":
        for path in (target, *target.rglob("*")):
            os.chown(path, 10001, 10001)
            if path.is_dir():
                path.chmod(0o700)
    release.sync_directory(target)


def verify_sources(target, index_digest, *, seen=None):
    target = Path(target)
    release.require(target.is_dir() and not target.is_symlink(), "migration_source_directory_invalid")
    actual = set()
    for path in target.rglob("*"):
        release.require(not path.is_symlink(), "migration_source_set_linked")
        if path.is_file():
            release.regular(path, maximum=64 * 1024**2)
            actual.add(path.relative_to(target).as_posix())
    if seen is not None:
        release.require(actual == seen, "migration_source_archive_changed")
    seen = actual
    release.require("index.json" in seen and release.checksum(target / "index.json") == index_digest,
                    "migration_source_index_changed")
    index = json.loads((target / "index.json").read_text())
    release.require(type(index) is dict and type(index.get("schema_version")) is int and index["schema_version"] == 1
        and index.get("purpose") == "local-unqualified-dstack-source-set"
        and index.get("production_adapter_verified") is False and type(index.get("sources")) is list
        and 0 < len(index["sources"]) <= 128, "migration_source_index_invalid")
    expected = {"index.json"}
    for item in index["sources"]:
        release.require(type(item) is dict and type(item.get("directory")) is str
            and item.get("gpu_count") == 1 and item.get("profile_slot_index") == 0
            and type(item.get("source_sha256")) is dict
            and set(item["source_sha256"]) == {"wangp-package.tar.gz", "wangp-bootstrap.py", "wangp-manifest.json", "wangp-runtime.json"},
            "migration_source_set_invalid")
        for name, digest in item["source_sha256"].items():
            relative = item["directory"] + "/" + name
            release.require(relative in seen and relative not in expected and type(digest) is str and release.DIGEST.fullmatch(digest)
                and release.checksum(target / relative) == digest, "migration_source_set_changed")
            expected.add(relative)
    release.require(seen == expected, "migration_source_set_extra_files")


def prepare(root, commit, platform_directory):
    incoming = root / "incoming-migrations" / commit
    value = manifest(incoming, commit, platform_manifest_sha256=release.checksum(platform_directory / "release-manifest.json"))
    approved(root, incoming, commit)
    parent = root / "migration" / "releases"
    protected_directory(parent)
    target = parent / commit
    if target.exists():
        protected_directory(target)
        release.require(manifest(target / "artifact", commit) == value, "migration_existing_release_differs")
        approved(root, target / "artifact", commit)
        verify_sources(target / "sources", value["source_index_sha256"])
        expected_overlay = (target / "artifact" / "platform-overlay.yaml").read_text().replace(
            "source: ../hatchet/entrypoint.sh", "source: " + json.dumps(str(target / "artifact" / "hatchet-entrypoint.sh")))
        release.regular(target / "overlay.yaml", root_owned=True, maximum=1024**2)
        release.require((target / "overlay.yaml").read_text() == expected_overlay, "migration_derived_overlay_changed")
        return target, value
    staging = Path(tempfile.mkdtemp(prefix=".migration-release-", dir=parent))
    try:
        artifact = staging / "artifact"; artifact.mkdir(mode=0o700)
        for name in FILES | {MANIFEST}:
            fd = os.open(incoming / name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(fd, "rb") as source, (artifact / name).open("xb") as output:
                info = os.fstat(source.fileno())
                release.require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1, "migration_copy_input_changed")
                maximum = 2 * 1024**3 if name == "dispatch-image.tar.gz" else 64 * 1024**2 if name == "source-set.tar.gz" else 1024**2
                copied = 0
                while chunk := source.read(1024 * 1024):
                    copied += len(chunk)
                    release.require(copied <= maximum, "migration_copy_input_grew")
                    output.write(chunk)
                output.flush(); os.fsync(output.fileno())
            (artifact / name).chmod(0o600)
        release.require(manifest(artifact, commit) == value, "migration_copied_release_differs")
        approved(root, artifact, commit)
        extract_sources(artifact / "source-set.tar.gz", staging / "sources", value["source_index_sha256"])
        overlay = (artifact / "platform-overlay.yaml").read_text()
        needle = "source: ../hatchet/entrypoint.sh"
        release.require(overlay.count(needle) == 1, "migration_entrypoint_binding_invalid")
        # Resolve only this reviewed relative bind; never interpolate credentials.
        overlay = overlay.replace(needle, "source: " + json.dumps(str(target / "artifact" / "hatchet-entrypoint.sh")))
        (staging / "overlay.yaml").write_text(overlay)
        (staging / "overlay.yaml").chmod(0o600)
        release.sync_directory(artifact); release.sync_directory(staging)
        staging.rename(target); release.sync_directory(parent)
        return target, value
    finally:
        if staging.exists():
            # Fresh helper-owned staging only; no persistent/user data.
            shutil.rmtree(staging)


def settings(root):
    directory = root / "migration"; protected_directory(directory)
    path = directory / "paths.json"
    release.regular(path, root_owned=True, maximum=32768)
    value = json.loads(path.read_text())
    release.require(type(value) is dict and set(value) == {"version", "environment", "host_memory_mib"}
        and type(value["version"]) is int and value["version"] == 1 and type(value["environment"]) is dict
        and PATH_FIELDS | OTHER_FIELDS <= set(value["environment"]) <= PATH_FIELDS | OTHER_FIELDS | OPTIONAL_FIELDS
        and type(value["host_memory_mib"]) is int and value["host_memory_mib"] >= 1024,
        "migration_paths_invalid")
    for key, text in value["environment"].items():
        if key in OPTIONAL_FIELDS and text == "":
            continue
        release.require(type(text) is str and text and text == text.strip() and not any(c in text for c in "\n\r\x00"),
                        "migration_path_reference_invalid")
        if key in PATH_FIELDS | OPTIONAL_PATH_FIELDS:
            path = Path(text)
            release.require(path.is_absolute() and ".." not in path.parts and not path.is_symlink(),
                            "migration_path_reference_invalid")
        elif key.endswith("_VOLUME"):
            release.require(re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}", text), "migration_volume_reference_invalid")
    env = value["environment"]
    storage_settings(env)
    release.require((env["SIXNINE_MIGRATION_GENERATION_ENABLED"], env["SIXNINE_MIGRATION_EXECUTION_BACKEND"])
        in {("0", "disabled"), ("1", "wangp-worker")}, "migration_admission_reference_invalid")
    release.require(re.fullmatch(r"[A-Za-z0-9_.-]+(?:,[A-Za-z0-9_.-]+)*", env["SIXNINE_OPERATOR_CAPACITY_OWNERS"]),
                    "migration_owner_reference_invalid")
    admins = env.get("SIXNINE_OPERATOR_ADMIN_OWNERS", "")
    release.require(not admins or re.fullmatch(r"[A-Za-z0-9_.-]+(?:,[A-Za-z0-9_.-]+)*", admins)
        and set(admins.split(",")) <= set(env["SIXNINE_OPERATOR_CAPACITY_OWNERS"].split(",")),
        "migration_admin_reference_invalid")
    release.require(re.fullmatch(r"[A-Za-z0-9_.-]{1,200}", env["SIXNINE_DEFAULT_DEPLOYMENT_PROFILE_ID"]),
                    "migration_default_profile_invalid")
    return value, release.checksum(directory / "paths.json")


def storage_settings(environment):
    """Closed nonsecret opt-in; existing local configurations need no changes."""
    provider = environment.get("SIXNINE_STORAGE_PROVIDER", "local") or "local"
    if provider == "local":
        release.require(not any(environment.get(key) for key in STORAGE_FIELDS - {"SIXNINE_STORAGE_PROVIDER"}),
            "migration_storage_local_configuration_invalid")
        return
    release.require(provider == "r2" and STORAGE_FIELDS <= set(environment)
        and all(environment.get(key) for key in STORAGE_FIELDS)
        and environment["SIXNINE_STORAGE_PROFILE"] == "cloudflare-r2--rig-root"
        and environment["SIXNINE_STORAGE_REGION"] == "auto"
        and re.fullmatch(r"https://[0-9a-f]{32}(?:\.(?:eu|us|fedramp))?\.r2\.cloudflarestorage\.com",
            environment["SIXNINE_STORAGE_ENDPOINT"])
        and re.fullmatch(r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]", environment["SIXNINE_STORAGE_BUCKET"])
        and environment["SIXNINE_STORAGE_CREDENTIAL_FILE"] == "/run/sixnine-secrets/r2_storage",
        "migration_storage_r2_configuration_invalid")


def default_profile(directory, environment):
    """The explicit creator default must have sources in this approved artifact."""
    index = json.loads((directory / "sources" / "index.json").read_text())
    release.require(environment["SIXNINE_DEFAULT_DEPLOYMENT_PROFILE_ID"] in
        {item["runtime_profile_id"] for item in index["sources"]}, "migration_default_profile_not_packaged")


def legacy_handoff(root, commit, *, active=None, now=None):
    """A separately approved CPU handoff does not settle or erase legacy pins.

    The fresh root receipt is required for initial activation. Later deployments
    bind the same receipt through active.json and recheck unchanged legacy pins;
    new dstack jobs/providers do not have to become empty for an app update.
    """
    directory = root / "migration"
    protected_directory(directory)
    receipt = directory / "legacy-cpu-handoff.json"
    approval = directory / "approved-legacy-cpu-handoff.sha256"
    release.regular(receipt, root_owned=True, maximum=32768)
    release.regular(approval, root_owned=True, maximum=128)
    digest = release.checksum(receipt)
    release.require(approval.read_text(encoding="ascii").strip() == digest,
                    "migration_legacy_handoff_independent_approval_missing")
    value = json.loads(receipt.read_text())
    release.require(type(value) is dict and set(value) == {"version", "purpose", "host_id", "target_commit",
        "checked_at", "markers_sha256", "providers", "business", "preservation", "evidence_refs"}
        and type(value["version"]) is int and value["version"] == 1
        and value["purpose"] == "legacy-cpu-authority-handoff-only"
        and type(value["target_commit"]) is str and release.SHA.fullmatch(value["target_commit"])
        and type(value["host_id"]) is str and re.fullmatch(r"i-[0-9a-f]{8,17}", value["host_id"])
        and type(value["checked_at"]) in (int, float) and math.isfinite(value["checked_at"])
        and type(value["markers_sha256"]) is dict and set(value["markers_sha256"]) == set(LEGACY_MARKERS)
        and type(value["providers"]) is dict and set(value["providers"]) == {"lium", "targon"}
        and type(value["business"]) is dict and set(value["business"]) == {"nonterminal_jobs", "nonterminal_attempts",
            "admission_closed_before_probe", "checked_at"}
        and all(type(value["business"][key]) is int and value["business"][key] == 0
            for key in ("nonterminal_jobs", "nonterminal_attempts"))
        and value["business"]["admission_closed_before_probe"] is True
        and type(value["business"]["checked_at"]) in (int, float) and math.isfinite(value["business"]["checked_at"])
        and value["preservation"] == {"original_markers": "unchanged", "ledger": "unchanged",
            "budgets_and_reservations": "unchanged", "provider_deletion": "not_asserted",
            "final_billing": "not_asserted", "legacy_retirement": "deferred"}
        and type(value["evidence_refs"]) is list and 0 < len(value["evidence_refs"]) <= 16
        and all(type(ref) is str and re.fullmatch(r"[A-Za-z0-9_.:/-]{1,300}", ref) for ref in value["evidence_refs"]),
        "migration_legacy_handoff_invalid")
    host = Path("/var/lib/cloud/data/instance-id").read_text().strip()
    release.require(value["host_id"] == host, "migration_legacy_handoff_wrong_host")
    for name, expected in value["markers_sha256"].items():
        path = root / name
        if expected is None:
            release.require(not path.exists() and not path.is_symlink(), "migration_legacy_marker_changed")
        else:
            release.require(type(expected) is str and release.DIGEST.fullmatch(expected), "migration_legacy_handoff_invalid")
            protected_directory(path.parent)
            release.regular(path, root_owned=True, maximum=16384)
            release.require(release.checksum(path) == expected, "migration_legacy_marker_changed")
    fresh = not active or active.get("current") is None
    now = time.time() if now is None else now
    if fresh:
        release.require(value["target_commit"] == commit and 0 <= now-value["checked_at"] <= 300,
                        "migration_legacy_handoff_stale")
        release.require(0 <= now-value["business"]["checked_at"] <= 300, "migration_legacy_business_observation_stale")
    else:
        release.require(active.get("legacy_cpu_handoff_sha256") == digest,
                        "migration_legacy_handoff_binding_changed")
    for provider in value["providers"].values():
        release.require(type(provider) is dict and set(provider) == {"account_scope_verified", "complete",
            "listed_allocations", "checked_at"} and provider["account_scope_verified"] is True
            and provider["complete"] is True and type(provider["listed_allocations"]) is int
            and provider["listed_allocations"] == 0 and type(provider["checked_at"]) in (int, float)
            and math.isfinite(provider["checked_at"]), "migration_legacy_account_observation_invalid")
        if fresh:
            release.require(0 <= now-provider["checked_at"] <= 300, "migration_legacy_account_observation_stale")
    return digest


def installed_checker(artifact):
    """Execute only independent-review installed root code, never artifact code."""
    helper = Path(__file__).resolve()
    checker_path = helper.with_name("migration_check.py")
    release.regular(helper, root_owned=True, maximum=1024**2)
    release.regular(checker_path, root_owned=True, maximum=1024**2)
    release.require(release.checksum(helper) == release.checksum(artifact / "maintenance.py")
        and release.checksum(checker_path) == release.checksum(artifact / "migration-check.py"),
        "migration_root_helpers_need_reviewed_update")
    spec = importlib.util.spec_from_file_location("sixnine_trusted_migration_checker", checker_path)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


def pinned_loaded_image(expected, environment):
    value = json.loads(release.command(["image", "inspect", expected["image"]], environment=environment))[0]
    candidates = [expected["image"] + "@" + item.split("@", 1)[1] for item in value.get("RepoDigests", []) if "@" in item]
    candidates.append(expected["image"] + "@" + value["Id"])
    for reference in candidates:
        try:
            pinned = json.loads(release.command(["image", "inspect", reference], environment=environment))[0]
        except release.ReleaseError:
            continue
        if pinned.get("Id") == value["Id"] and pinned["Id"] in expected["archive_image_ids"]:
            return reference
    raise release.ReleaseError("migration_loaded_image_digest_not_resolvable")


def compose(base, migration_directory, environment, *arguments, timeout=180):
    return release.command(["compose", "--env-file", "/dev/null", "--project-directory", str(base),
        "-f", str(base / "compose.yaml"), "-f", str(migration_directory / "overlay.yaml"), *arguments],
        environment=environment, timeout=timeout)


def verify_services(base, directory, environment, app, dispatch):
    expected = {"app": app, "dispatch": dispatch, "controller": dispatch}
    values = {}
    for name in ("app", *SHARED, "hatchet-db"):
        identity = compose(base, directory, environment, "ps", "--all", "--quiet", name, timeout=20).decode().strip()
        release.require(re.fullmatch(r"[0-9a-f]{12,64}", identity), "migration_cpu_service_not_unique")
        inspected = json.loads(release.command(["inspect", identity], environment=environment, timeout=20))
        release.require(type(inspected) is list and len(inspected) == 1, "migration_cpu_service_not_unique")
        value = inspected[0]
        labels = value.get("Config", {}).get("Labels", {})
        release.require(labels.get("com.docker.compose.project") == "sixnine-platform"
            and labels.get("com.docker.compose.service") == name, "migration_cpu_service_identity_mismatch")
        values[name] = value
    app_id = values["app"]["Id"]
    for name, value in values.items():
        release.require(value.get("State", {}).get("Running") is True
            and value.get("State", {}).get("Restarting") is not True, "migration_cpu_service_not_running")
        if name in expected:
            release.require(value.get("Image") in expected[name]["archive_image_ids"], "migration_cpu_image_mismatch")
        if name in SHARED:
            release.require(value.get("HostConfig", {}).get("NetworkMode") in {
                "container:" + app_id, "container:" + values["app"].get("Name", "").lstrip("/")},
                "migration_shared_namespace_stale")
        if name in {"app", "dstack", "hatchet"}:
            release.require(value.get("State", {}).get("Health", {}).get("Status") == "healthy",
                            "migration_cpu_service_not_healthy")
    for service, volume, destination in (("hatchet", "SIXNINE_HATCHET_CONFIG_VOLUME", "/config"),
            ("hatchet-db", "SIXNINE_HATCHET_POSTGRES_VOLUME", "/var/lib/postgresql/data")):
        mounts = [item for item in values[service].get("Mounts", []) if item.get("Destination") == destination]
        release.require(len(mounts) == 1 and mounts[0].get("Type") == "volume"
            and mounts[0].get("Name") == environment[volume], "migration_broker_volume_changed")
    for service, image in (("dstack", "dstackai/dstack:0.22.3@sha256:ce0e567675360ba91a6867a0399dccc1682dc2169075c6210d2ec873146f1ad8"),
            ("hatchet", "ghcr.io/hatchet-dev/hatchet/hatchet-lite:v0.110.5@sha256:cc81f078a5285d7d2051ec4a4fa459d033e1face4ca5983d82612b00da6e3fa3"),
            ("hatchet-db", "postgres:15@sha256:c961aa287d8698297cb26cdfadfbe9fd2cbaf77e53cfffe9636e8d8a1e4d842c")):
        pinned = json.loads(release.command(["image", "inspect", image], environment=environment, timeout=20))[0]
        release.require(values[service].get("Image") == pinned.get("Id"), "migration_infrastructure_image_mismatch")


def guard_legacy(root, *, commit=None, active=None):
    receipt = root / "migration" / "legacy-cpu-handoff.json"
    digest = None
    if receipt.exists() or receipt.is_symlink():
        digest = legacy_handoff(root, commit, active=active)
    else:
        release.require(not active or not active.get("legacy_cpu_handoff_sha256"),
                        "migration_legacy_handoff_binding_changed")
        release.require(release.gpu_deployment_context(root) is None, "migration_requires_legacy_controller_handoff")
    environment = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8",
        "DOCKER_CONFIG": "/opt/sixnine-release/docker-config"}
    if digest and (not active or active.get("current") is None):
        # Initial handoff uses bounded downtime, so its final zero-job probe
        # cannot race a new request accepted by the old app after the receipt.
        app = release.command(["ps", "--quiet", "--filter", "label=com.docker.compose.project=sixnine-platform",
            "--filter", "label=com.docker.compose.service=app"], environment=environment, timeout=20)
        release.require(not app.strip(), "migration_legacy_admission_not_fenced")
    running = release.command(["ps", "--quiet", "--filter", "name=^sixnine-operator-"],
        environment=environment, timeout=20)
    release.require(not running.strip(), "migration_legacy_controller_still_running")
    for service in ("operator-controller", "gpu-controller", "gpu-worker"):
        running = release.command(["ps", "--quiet", "--filter", "label=com.docker.compose.project=sixnine-platform",
            "--filter", "label=com.docker.compose.service=" + service], environment=environment, timeout=20)
        release.require(not running.strip(), "migration_legacy_controller_still_running")
    running = release.command(["ps", "--quiet", "--filter", "name=^sixnine-(finite|acceptance)-"],
        environment=environment, timeout=20)
    release.require(not running.strip(), "migration_legacy_controller_still_running")
    return digest


def apply_locked(root, commit):
    """Caller holds existing release lock. Reconnect every shared CPU member."""
    config, config_digest = settings(root)
    active_path = root / "migration" / "active.json"
    release.regular(active_path, root_owned=True, maximum=16384)
    active = json.loads(active_path.read_text())
    release.require(type(active) is dict and type(active.get("version")) is int and active["version"] == 1 and active.get("active") is True
        and active.get("paths_sha256") == config_digest, "migration_configuration_changed_requires_review")
    handoff_digest = guard_legacy(root, commit=commit, active=active)
    release.approved_manifest(root, root / "incoming" / commit, commit)
    base = release.prepare_bundle(root, commit)
    directory, migration = prepare(root, commit, base)
    checker = installed_checker(directory / "artifact")
    env = release.deployment_environment(root / "site.env", commit)
    state = json.loads((root / "release-state.json").read_text())
    release.require(state.get("pending") in (None, commit), "another_release_requires_reconciliation")
    release.require(state.get("dependencies") == release.pinned_dependencies(env),
                    "dependency_change_requires_separate_maintenance")
    app = release.load_approved_image(root, base, commit, env)
    dispatch = {"commit": commit, **migration["dispatch"]}
    dispatch["archive_image_ids"] = release.validate_image_archive(directory / "artifact" / "dispatch-image.tar.gz", dispatch)
    release.command(["load", "--input", str(directory / "artifact" / "dispatch-image.tar.gz")], environment=env, timeout=300)
    env.update(config["environment"], SIXNINE_IMAGE=pinned_loaded_image(app, env),
        SIXNINE_DISPATCH_IMAGE=pinned_loaded_image(dispatch, env), SIXNINE_DSTACK_SOURCE_DIR=str(directory / "sources"))
    default_profile(directory, env)
    checker.check(base / "compose.yaml", directory / "overlay.yaml", environment=env,
        host_memory_mib=config["host_memory_mib"], check_inputs=True)
    if state.get("current") == commit and state.get("status") == "app_ready" and active.get("current") == commit:
        verify_services(base, directory, env, app, dispatch)
        checker.check(base / "compose.yaml", directory / "overlay.yaml", environment=env,
            host_memory_mib=config["host_memory_mib"], check_inputs=True, probe=True)
        return
    previous = state.get("current")
    release.write_state(root, {**state, "pending": commit, "status": "deploying_migration", "updated_at": time.time()})
    atomic(active_path, {**active, "pending": commit})
    try:
        # Stop only the four CPU members before replacing their shared namespace.
        # Broker volumes and native provider runs/attempts remain untouched.
        compose(base, directory, env, "stop", *SHARED, timeout=240)
        compose(base, directory, env, "up", "-d", "db", "hatchet-db", timeout=240)
        compose(base, directory, env, "run", "--rm", "db-init", timeout=120)
        release.retire_frontend_pointer(root)
        compose(base, directory, env, "up", "-d", "--no-deps", "--force-recreate", "app", *SHARED, timeout=300)
        release.wait_ready(base, env)
        deadline = time.monotonic() + 180
        while True:
            try:
                verify_services(base, directory, env, app, dispatch)
                checker.check(base / "compose.yaml", directory / "overlay.yaml", environment=env,
                    host_memory_mib=config["host_memory_mib"], check_inputs=True, probe=True)
                break
            except (release.ReleaseError, ValueError):
                if time.monotonic() >= deadline:
                    raise
                time.sleep(3)
        compose(base, directory, env, "up", "-d", "--no-deps", "caddy")
        release.wait_proxy_stable(base, env)
        atomic(active_path, {"version": 1, "active": True, "paths_sha256": config_digest,
            "current": commit, "pending": None,
            **({"legacy_cpu_handoff_sha256": handoff_digest} if handoff_digest else {})})
        release.write_state(root, {"current": commit, "previous": previous if previous != commit else state.get("previous"),
            "dependencies": state["dependencies"], "status": "app_ready", "updated_at": time.time()})
    except Exception:
        release.write_state(root, {**state, "pending": commit, "status": "migration_failed_needs_reconciliation",
            "updated_at": time.time()})
        raise release.ReleaseError("migration_release_failed_original_state_preserved") from None


def activate(root, commit):
    """Explicit independent root maintenance; future ordinary deploys stay guarded."""
    release.check_host(root)
    import fcntl
    with (root / "release.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _, digest = settings(root)
        path = root / "migration" / "active.json"
        if path.exists() or path.is_symlink():
            release.regular(path, root_owned=True, maximum=16384)
            active = json.loads(path.read_text())
            release.require(active.get("active") is True and active.get("paths_sha256") == digest,
                            "migration_activation_requires_reconciliation")
        handoff_digest = guard_legacy(root, commit=commit, active=active if path.exists() else None)
        import fetch_release_s3
        fetch_release_s3.fetch(commit, root=root)
        fetch(root, commit)
        if not path.exists():
            atomic(path, {"version": 1, "active": True, "paths_sha256": digest, "current": None, "pending": commit,
                **({"legacy_cpu_handoff_sha256": handoff_digest} if handoff_digest else {})})
        apply_locked(root, commit)


def main(argv=None):
    import sys
    argv = sys.argv[1:] if argv is None else argv
    try:
        release.require(len(argv) == 2 and argv[0] == "activate" and release.SHA.fullmatch(argv[1]),
                        "migration_activate_commit_required")
        activate(release.ROOT, argv[1])
        print(json.dumps({"state": "approved_migration_release_healthy", "commit": argv[1]}))
        return 0
    except Exception:
        print("Migration maintenance incomplete; preserve state and reconcile the same commit", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
