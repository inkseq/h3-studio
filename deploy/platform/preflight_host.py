#!/usr/bin/python3
"""Read-only Linux host metadata checks; never read secrets or start services.

Install this reviewed file beside the root-owned release controller. It accepts
no arbitrary CLI paths, writes no files, and never inspects registry credentials.
"""
from __future__ import annotations

import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess

ROOT = Path("/srv/sixnine")
SECRETS = Path("/run/sixnine-secrets")
DOCKER_CONFIG = Path("/opt/sixnine-release/docker-config")
SAFE_PATH = "/usr/sbin:/usr/bin:/sbin:/bin"
GIB = 1024**3


class PreflightError(Exception):
    """Stable non-secret diagnostic code."""


def require(value, code):
    if not value:
        raise PreflightError(code)


def metadata(path):
    value = path.lstat()
    return {"mode": value.st_mode, "uid": value.st_uid, "gid": value.st_gid,
            "links": value.st_nlink, "bytes": value.st_size}


def _directory(info, code, *, owner=0, group=None, mode=None, writable_group=False):
    require(stat.S_ISDIR(info["mode"]) and info["uid"] == owner, code)
    if group is not None:
        require(info["gid"] == group, code)
    bits = stat.S_IMODE(info["mode"])
    require(not bits & (0o002 if writable_group else 0o022), code)
    if mode is not None:
        require(bits == mode, code)


def validate_docker_config(entries):
    """Allow no CLI settings; only empty, private Buildx-created scaffolding."""
    code = "preflight_docker_config_must_be_empty"
    directories = {"buildx", "buildx/activity", "buildx/defaults", "buildx/instances"}
    require(type(entries) is dict and set(entries) <= directories | {"buildx/.lock"}, code)
    require(not entries or "buildx" in entries, code)
    for name, info in entries.items():
        if name in directories:
            _directory(info, code, group=0, mode=0o700)
        else:
            require(stat.S_ISREG(info["mode"]) and (info["uid"], info["gid"]) == (0, 0)
                    and stat.S_IMODE(info["mode"]) == 0o600 and info["links"] == 1
                    and info["bytes"] == 0, code)


def docker_config_metadata(directory):
    """Inspect bounded names/types without following links or reading files."""
    code = "preflight_docker_config_must_be_empty"

    def names(path, maximum):
        result = []
        with os.scandir(path) as scan:
            for entry in scan:
                result.append(entry.name)
                require(len(result) <= maximum, code)
        return result

    top = names(directory, 1)
    require(not top or top == ["buildx"], code)
    if not top:
        return {}
    buildx = directory / "buildx"
    entries = {"buildx": metadata(buildx)}
    validate_docker_config(entries)  # Reject a linked/unprotected parent before traversal.
    for name in names(buildx, 4):
        require(name in {"activity", "defaults", "instances", ".lock"}, code)
        relative = "buildx/" + name
        entries[relative] = metadata(buildx / name)
        validate_docker_config(entries)
        if name != ".lock":
            names(buildx / name, 0)  # Even one instance/activity/config file is unapproved.
    return entries


def validate_snapshot(snapshot):
    """Pure validation used by fake-host tests; input is metadata, never content."""
    require(snapshot["platform"] == "posix" and snapshot["euid"] == 0, "preflight_requires_linux_root")
    for info in snapshot["parents"]:
        _directory(info, "preflight_parent_not_root_controlled")
    dirs = snapshot["directories"]
    require(set(dirs) == {"root", "incoming", "releases", "approved-releases", "platform-data",
                         "upload-spool", "postgres", "secret-root", "docker-config"}, "preflight_directory_set_invalid")
    for name in ("root", "releases", "approved-releases"):
        _directory(dirs[name], "preflight_release_directory_not_protected")
    _directory(dirs["incoming"], "preflight_incoming_directory_not_protected", writable_group=True)
    for name in ("platform-data", "upload-spool"):
        # Private writable directories for the actual non-root app identity.
        info = dirs[name]
        require(stat.S_ISDIR(info["mode"]) and (info["uid"], info["gid"]) == (10001, 10001)
                and stat.S_IMODE(info["mode"]) == 0o700, "preflight_app_directory_owner_or_mode")
    pg = dirs["postgres"]
    # PGDATA is a CHILD of this bind root. The entrypoint chowns PGDATA only;
    # a root:root 0700 parent remains inaccessible after dropping privileges.
    # Match the reviewed official PostgreSQL 17 Alpine (70) / Debian (999) UID.
    require(stat.S_ISDIR(pg["mode"]) and (pg["uid"], pg["gid"]) in {(70, 70), (999, 999)}
            and stat.S_IMODE(pg["mode"]) == 0o700, "preflight_postgres_directory_owner_or_mode")
    for name in ("secret-root", "docker-config"):
        _directory(dirs[name], "preflight_private_root_directory_required", group=0, mode=0o700)
    validate_docker_config(snapshot["docker_config_entries"])
    names = set(snapshot["secrets"])
    require(names in ({"db_admin_password", "app_database_url"},
                      {"db_admin_password", "app_database_url", "google_titles"}), "preflight_secret_set_invalid")
    for name, info in snapshot["secrets"].items():
        require(stat.S_ISREG(info["mode"]) and info["uid"] == 0 and info["links"] == 1
                and 1 <= info["bytes"] <= 16384, "preflight_secret_file_not_protected_regular")
        if name == "db_admin_password":
            allowed, group = {0o400, 0o600}, 0
        else:
            allowed, group = {0o440, 0o640}, 10001
        require(stat.S_IMODE(info["mode"]) in allowed and info["gid"] == group,
                "preflight_secret_owner_or_mode")
    require(snapshot["secret_filesystems"] == {name: "tmpfs" for name in names | {"secret-root"}},
            "preflight_runtime_secrets_require_tmpfs")
    docker = snapshot["docker"]
    require(docker.get("os") == "linux" and type(docker.get("cpus")) is int and docker["cpus"] >= 2,
            "preflight_docker_requires_two_linux_cpus")
    # A nominal 4-GiB EC2 host reports less usable RAM after kernel reservation.
    # Reviewed steady containers: app 2 GiB + PG 512 MiB + Caddy 128 MiB.
    # No local generation/render worker is covered by this initial host profile.
    require(type(docker.get("memory_bytes")) is int and docker["memory_bytes"] >= 3584*1024**2,
            "preflight_docker_memory_below_reviewed_floor")
    return {"state": "host_metadata_ready", "cpu_count": docker["cpus"],
            "secret_contents_checked": False, "services_started": False,
            "remaining": ["dependency_images_and_archive_identity", "subnet_and_port_conflicts",
                          "approved_runtime_secret_source", "disk_capacity_and_backups", "formal_accounts",
                          "public_dns_tls_and_user_acceptance"]}


def _mount_path(value):
    # /proc/self/mountinfo escapes whitespace and backslashes in mount points.
    return PurePosixPath(re.sub(r"\\(040|011|012|134)", lambda m: chr(int(m[1], 8)), value))


def filesystem_for(path, mountinfo):
    path = PurePosixPath(path.as_posix())
    found = []
    for line in mountinfo.splitlines():
        before, separator, after = line.partition(" - ")
        fields, filesystem = before.split(), after.split()
        if not separator or len(fields) < 6 or not filesystem:
            raise PreflightError("preflight_mountinfo_invalid")
        mount = _mount_path(fields[4])
        if mount.is_absolute() and path.is_relative_to(mount):
            found.append((len(mount.parts), filesystem[0]))
    require(bool(found), "preflight_mount_not_identified")
    # A bind-mounted persistent secret file overrides a tmpfs parent and fails.
    return max(found, key=lambda item: item[0])[1]


def check_host(*, root=ROOT, secret_root=SECRETS, docker_config=DOCKER_CONFIG):
    """Read only fixed trusted host metadata and local Docker capacity."""
    require(os.name == "posix" and os.geteuid() == 0, "preflight_requires_linux_root")
    require((root, secret_root, docker_config) == (ROOT, SECRETS, DOCKER_CONFIG), "preflight_unreviewed_host_paths")
    paths = {"root": root, **{name: root/name for name in ("incoming", "releases", "approved-releases",
             "platform-data", "upload-spool", "postgres")}, "secret-root": secret_root, "docker-config": docker_config}
    try:
        parents = set(root.parents) | set(secret_root.parents) | set(docker_config.parents)
        snapshot = {"platform": os.name, "euid": os.geteuid(), "parents": [metadata(path) for path in parents],
                    "directories": {name: metadata(path) for name, path in paths.items()}}
        # Validate parent chains before looking below them; never traverse a link.
        for info in snapshot["parents"]:
            _directory(info, "preflight_parent_not_root_controlled")
        for name in ("secret-root", "docker-config"):
            _directory(snapshot["directories"][name], "preflight_private_root_directory_required", group=0, mode=0o700)
        snapshot["docker_config_entries"] = docker_config_metadata(docker_config)
        names = ["db_admin_password", "app_database_url"]
        if (secret_root/"google_titles").exists() or (secret_root/"google_titles").is_symlink():
            names.append("google_titles")
        snapshot["secrets"] = {name: metadata(secret_root/name) for name in names}
        with Path("/proc/self/mountinfo").open("r", encoding="utf-8") as source:
            mounts = source.read(1024*1024+1)
        require(len(mounts) <= 1024*1024, "preflight_mountinfo_too_large")
        snapshot["secret_filesystems"] = {name: filesystem_for(path, mounts) for name, path in {
            "secret-root": secret_root, **{name: secret_root/name for name in names}}.items()}
        # No registry/login/context/plugin settings are accepted. Docker can
        # leave only its private empty Buildx scaffold after Compose commands.
        # Only the local daemon is queried; no network command is issued.
        info = subprocess.run(["/usr/bin/docker", "--host", "unix:///var/run/docker.sock", "info",
                               "--format", "{{.OSType}} {{.NCPU}} {{.MemTotal}}"],
            env={"PATH": SAFE_PATH, "LANG": "C.UTF-8", "DOCKER_CONFIG": str(docker_config)},
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, timeout=15, check=True).stdout.strip().split()
        require(len(info) == 3, "preflight_docker_capacity_invalid")
        snapshot["docker"] = {"os": info[0], "cpus": int(info[1]), "memory_bytes": int(info[2])}
        return validate_snapshot(snapshot)
    except PreflightError:
        raise
    except Exception:
        raise PreflightError("preflight_host_metadata_unavailable") from None


def main():
    try:
        print(json.dumps(check_host()))
        return 0
    except PreflightError as error:
        print(json.dumps({"state": "host_preflight_refused", "reason": str(error)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
